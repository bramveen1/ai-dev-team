"""Router configuration — agent discovery and environment variable loading.

Agents are discovered by scanning ``config/agents/*/agent.yaml``. Each
manifest is the single source of truth for one agent (identity, capabilities,
scheduled tasks). Adding or removing an agent is a directory operation, not
a code change.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
AGENTS_DIR = CONFIG_DIR / "agents"

SHARED_WORLDVIEW_FILE = "config/shared/WORLDVIEW.md"
SHARED_MEMORY_FILE = "config/shared/MEMORY.md"

DEFAULTS = {
    "session_timeout": 600,
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
    """
    base = agents_dir if agents_dir is not None else AGENTS_DIR
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
        "slack_credentials": load_slack_credentials(list(agent_map.keys())),
        "session_timeout": int(os.environ.get("SESSION_TIMEOUT", DEFAULTS["session_timeout"])),
        "log_level": os.environ.get("LOG_LEVEL", DEFAULTS["log_level"]),
        "agent_map": agent_map,
    }

    logger.debug(
        "Loaded config: agents_with_creds=%d, session_timeout=%d",
        len(cfg["slack_credentials"]),
        cfg["session_timeout"],
    )
    return cfg
