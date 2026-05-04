"""Router configuration — agent map and environment variable loading."""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Path to the agent tools configuration file
AGENT_TOOLS_PATH = Path(__file__).parent.parent / "config" / "agent_tools.json"

# Agent definitions. Each entry maps a logical agent name to its configuration.
# Structure supports adding more agents without refactoring — just add entries.
AGENT_MAP = {
    "lisa": {
        "name": "Lisa",
        "container": "lisa",
        "role_file": "config/agents/lisa/role.md",
        "personality_file": "config/agents/lisa/personality.md",
        "thinking_status": "is reviewing findings\u2026",
    },
    # Future agents:
    # "alex": {"name": "Alex", "container": "alex", "role_file": "config/agents/alex/role.md",
    #          "personality_file": "config/agents/alex/personality.md",
    #          "thinking_status": "Thinking through this\u2026"},
    # "sam": {"name": "Sam", "container": "sam", "role_file": "config/agents/sam/role.md",
    #         "personality_file": "config/agents/sam/personality.md"},
    # "dave": {"name": "Dave", "container": "dave", "role_file": "config/agents/dave/role.md",
    #          "personality_file": "config/agents/dave/personality.md"},
    # "maya": {"name": "Maya", "container": "maya", "role_file": "config/agents/maya/role.md",
    #          "personality_file": "config/agents/maya/personality.md"},
    # "lin": {"name": "Lin", "container": "lin", "role_file": "config/agents/lin/role.md",
    #         "personality_file": "config/agents/lin/personality.md"},
}

# Shared context files loaded by all agents
SHARED_WORLDVIEW_FILE = "config/shared/WORLDVIEW.md"
SHARED_MEMORY_FILE = "config/shared/MEMORY.md"

# Default configuration values
DEFAULTS = {
    "session_timeout": 600,
    "max_token_budget": 4000,
    "log_level": "INFO",
}


def get_agent_map() -> dict:
    """Return the agent map dictionary.

    Each key is an agent name, and each value is a dict with:
        - name: Display name of the agent
        - container: Docker container/service name
        - role_file: Path to the agent's role definition file
    """
    return dict(AGENT_MAP)


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
        - max_token_budget: Maximum token budget for context assembly
        - log_level: Logging level string
        - agent_map: The agent configuration map
    """
    agent_map = get_agent_map()
    cfg = {
        "slack_credentials": load_slack_credentials(list(agent_map.keys())),
        "session_timeout": int(os.environ.get("SESSION_TIMEOUT", DEFAULTS["session_timeout"])),
        "max_token_budget": int(os.environ.get("MAX_TOKEN_BUDGET", DEFAULTS["max_token_budget"])),
        "log_level": os.environ.get("LOG_LEVEL", DEFAULTS["log_level"]),
        "agent_map": agent_map,
    }

    logger.debug(
        "Loaded config: agents_with_creds=%d, session_timeout=%d, max_token_budget=%d",
        len(cfg["slack_credentials"]),
        cfg["session_timeout"],
        cfg["max_token_budget"],
    )
    return cfg


def load_agent_tools(path: str | Path | None = None) -> dict[str, list[str]]:
    """Load the agent-to-system-docs mapping from config/agent_tools.json.

    Args:
        path: Optional path to the JSON file. Defaults to AGENT_TOOLS_PATH.

    Returns:
        A dict mapping agent names to lists of system doc filenames.
        Returns an empty dict if the file is missing or invalid.
    """
    config_path = Path(path) if path else AGENT_TOOLS_PATH
    try:
        with open(config_path) as f:
            data = json.load(f)
        logger.debug("Loaded agent tools config: %d agents", len(data))
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        logger.warning("Could not load agent tools config from %s: %s", config_path, e)
        return {}
