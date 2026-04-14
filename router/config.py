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
SHARED_SOUL_FILE = "config/shared/SOUL.md"
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


def load_config() -> dict:
    """Load configuration from environment variables with sensible defaults.

    Returns a dict with:
        - slack_bot_token: Slack bot OAuth token
        - slack_app_token: Slack app-level token (for Socket Mode)
        - slack_signing_secret: Slack signing secret
        - session_timeout: Seconds before an idle session times out
        - max_token_budget: Maximum token budget for context assembly
        - log_level: Logging level string
        - agent_map: The agent configuration map
    """
    cfg = {
        "slack_bot_token": os.environ.get("SLACK_BOT_TOKEN", ""),
        "slack_app_token": os.environ.get("SLACK_APP_TOKEN", ""),
        "slack_signing_secret": os.environ.get("SLACK_SIGNING_SECRET", ""),
        "session_timeout": int(os.environ.get("SESSION_TIMEOUT", DEFAULTS["session_timeout"])),
        "max_token_budget": int(os.environ.get("MAX_TOKEN_BUDGET", DEFAULTS["max_token_budget"])),
        "log_level": os.environ.get("LOG_LEVEL", DEFAULTS["log_level"]),
        "agent_map": get_agent_map(),
    }

    logger.debug(
        "Loaded config: session_timeout=%d, max_token_budget=%d", cfg["session_timeout"], cfg["max_token_budget"]
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
