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
    # 1800s (30 min) — raised from 600s to accommodate --max-turns 50 on Sonnet
    # (~20-25s/turn → 50 turns ≈ 17-21 min wall clock).  See issue #200.
    # Override per-agent via container_timeout in agent.yaml, or globally via
    # the SESSION_TIMEOUT env var.
    "session_timeout": 1800,
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


def load_slack_credentials(agent_map: dict[str, dict]) -> dict[str, dict[str, str]]:
    """Load per-agent Slack credentials from ``agent_map``.

    Two modes, per agent:

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

    Two modes, per agent:

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
    ``SESSION_TIMEOUT`` env var, falling back to
    ``DEFAULTS["session_timeout"]``. Both :func:`load_config` and the
    merge-queue idle guard call this so routing, cleanup, and idle
    detection share one expiry boundary (issue #462).
    """
    return int(os.environ.get("SESSION_TIMEOUT", DEFAULTS["session_timeout"]))


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
    agent_map = get_agent_map()
    cfg = {
        "slack_credentials": load_slack_credentials(agent_map),
        "session_timeout": resolve_session_timeout(),
        "log_level": os.environ.get("LOG_LEVEL", DEFAULTS["log_level"]),
        "agent_map": agent_map,
    }

    logger.debug(
        "Loaded config: agents_with_creds=%d, session_timeout=%d",
        len(cfg["slack_credentials"]),
        cfg["session_timeout"],
    )
    return cfg
