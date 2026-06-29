"""Router configuration — agent discovery and environment variable loading.

Agents are discovered by scanning ``config/agents/*/agent.yaml``. Each
manifest is the single source of truth for one agent (identity, capabilities,
scheduled tasks). Adding or removing an agent is a directory operation, not
a code change.

Slack credentials are loaded from ``config/agents.yaml`` when that file exists
(or when ``AGENTS_CONFIG`` env var points to one); otherwise the legacy flat
env-var approach (``<NAME>_BOT_TOKEN`` etc.) is used as a fallback.
"""

from __future__ import annotations

import logging
import os
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

_agent_map_cache: dict[str, dict] | None = None


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

        discovered[agent_id] = {
            "name": display_name,
            "container": container,
            "role_file": f"config/agents/{agent_id}/role.md",
            "personality_file": f"config/agents/{agent_id}/personality.md",
            "thinking_status": manifest.get("thinking_status", ""),
            "model": manifest.get("model") or None,
            # None → fall back to the global session_timeout / SESSION_TIMEOUT env var.
            "container_timeout": manifest.get("container_timeout"),
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


def load_slack_credentials(agent_names: list[str]) -> dict[str, dict[str, str]]:
    """Load per-agent Slack credentials from ``<NAME>_BOT_TOKEN`` env vars.

    For each agent, looks up ``<AGENT>_BOT_TOKEN``, ``<AGENT>_APP_TOKEN``, and
    ``<AGENT>_SIGNING_SECRET`` (agent name uppercased). Agents missing any of
    the three are skipped with a warning, so the router can still start when
    a newly added agent's Slack app isn't fully configured yet.
    """
    credentials: dict[str, dict[str, str]] = {}
    for agent_name in agent_names:
        prefix = agent_name.upper()
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
            logger.warning("Skipping agent '%s' — missing Slack env vars: %s", agent_name, ", ".join(missing))
            continue

        credentials[agent_name] = {
            "bot_token": bot_token,
            "app_token": app_token,
            "signing_secret": signing_secret,
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


def find_agents_config_path() -> Path | None:
    """Return the path to agents.yaml, or ``None`` if not configured.

    Resolution order:
    1. ``AGENTS_CONFIG`` env var (explicit path — highest priority).
    2. ``config/agents.yaml`` relative to the repo root (default location).

    Returns ``None`` when neither exists, triggering the flat env-var fallback.
    """
    env_path = os.environ.get("AGENTS_CONFIG", "").strip()
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
        logger.warning("AGENTS_CONFIG points to a missing file: %s — falling back to env vars", env_path)
        return None

    default = REPO_ROOT / "config" / "agents.yaml"
    return default if default.exists() else None


def _load_slack_creds(agent_names: list[str]) -> dict[str, dict[str, str]]:
    """Load Slack credentials from agents.yaml when available; fall back to env vars.

    When ``agents.yaml`` is found, structural errors propagate immediately
    (fail loud at startup). Individual agents with unresolvable secrets are
    soft-skipped with a warning in both paths.
    """
    agents_config_path = find_agents_config_path()
    if agents_config_path is not None:
        from router.agents_config import AgentsConfigError, load_slack_credentials_from_yaml

        logger.debug("Loading Slack credentials from agents.yaml: %s", agents_config_path)
        try:
            return load_slack_credentials_from_yaml(agents_config_path)
        except AgentsConfigError:
            raise

    return load_slack_credentials(agent_names)


def load_config() -> dict:
    """Load configuration from environment variables with sensible defaults.

    Returns a dict with:
        - slack_credentials: ``{agent_name: {bot_token, app_token, signing_secret}}``
        - session_timeout: Seconds before an idle session times out
        - log_level: Logging level string
        - agent_map: The agent configuration map

    Slack credentials are loaded from ``agents.yaml`` when that file is
    present; the flat ``<NAME>_BOT_TOKEN`` env-var approach is used as a
    fallback for environments that have not yet migrated.

    Note: the context token budget is owned by ``router.dispatcher``. It reads
    the ``MAX_CONTEXT_TOKENS`` env var (with a sane default) and is the single
    source of truth — adding a second copy here is what caused issue #144.
    """
    agent_map = get_agent_map()
    cfg = {
        "slack_credentials": _load_slack_creds(list(agent_map.keys())),
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
