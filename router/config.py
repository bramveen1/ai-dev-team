"""Router configuration — agent map and environment variable loading."""

import logging
import os

logger = logging.getLogger(__name__)

# Agent definitions. Each entry maps a logical agent name to its configuration.
# Structure supports adding more agents without refactoring — just add entries.
AGENT_MAP = {
    "lisa": {
        "name": "Lisa",
        "container": "lisa",
        "role_file": "agents/lisa/role.md",
    },
    # Future agents:
    # "max": {"name": "Max", "container": "max", "role_file": "agents/max/role.md"},
    # "sara": {"name": "Sara", "container": "sara", "role_file": "agents/sara/role.md"},
    # "kai": {"name": "Kai", "container": "kai", "role_file": "agents/kai/role.md"},
    # "dev": {"name": "Dev", "container": "dev", "role_file": "agents/dev/role.md"},
    # "ops": {"name": "Ops", "container": "ops", "role_file": "agents/ops/role.md"},
}

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
