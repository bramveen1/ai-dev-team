"""Structured identity model — agents.yaml schema, secret resolution, and validation.

Replaces flat env-var sprawl (SAM_SLACK_BOT_TOKEN, SAM_DISCORD_APP_ID, …) with a
versioned YAML that declares each agent's identity on each backend, referencing
secrets by name rather than embedding them.

Secret syntax: ``${SECRET:ENV_VAR_NAME}`` — resolved against env vars by default.
The resolver is pluggable so tests and future secret-store integrations can swap it.

Startup validation: ``load_agents_yaml()`` raises :class:`AgentsConfigError` on any
structural violation (bad YAML, wrong version, missing required fields, duplicate
agent IDs). Unresolvable secrets are soft-skipped with a warning at credential-load
time, matching the behaviour of :func:`router.config.load_slack_credentials`.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Callable

import yaml

logger = logging.getLogger(__name__)

SECRET_PATTERN = re.compile(r"\$\{SECRET:([^}]+)\}")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AGENTS_CONFIG_PATH = REPO_ROOT / "config" / "agents.yaml"

# Supported schema version.
SCHEMA_VERSION = "1"


class AgentsConfigError(ValueError):
    """Raised when agents.yaml is missing, malformed, or structurally invalid."""


# ---------------------------------------------------------------------------
# Secret resolution
# ---------------------------------------------------------------------------


def resolve_secret_refs(
    value: str,
    resolver: Callable[[str], str | None] | None = None,
) -> str:
    """Resolve ``${SECRET:name}`` placeholders in *value*.

    Each placeholder is replaced with the result of ``resolver(name)``.
    Raises :class:`AgentsConfigError` if *resolver* returns ``None`` for any
    name — the caller decides whether to propagate or soft-skip.
    """
    if resolver is None:
        resolver = os.environ.get

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        resolved = resolver(name)
        if resolved is None:
            raise AgentsConfigError(
                f"Secret '{name}' not found (referenced as ${{SECRET:{name}}}); "
                "set the corresponding env var or configure a secrets store."
            )
        return resolved

    return SECRET_PATTERN.sub(_replace, value)


def _resolve_obj(
    obj: Any,
    resolver: Callable[[str], str | None] | None = None,
) -> Any:
    """Recursively resolve ``${SECRET:name}`` references in any YAML structure."""
    if isinstance(obj, str):
        return resolve_secret_refs(obj, resolver)
    if isinstance(obj, dict):
        return {k: _resolve_obj(v, resolver) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_obj(item, resolver) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def _validate_schema(data: Any, path: Path) -> None:
    """Validate the structure of *data* against the agents.yaml schema.

    Raises :class:`AgentsConfigError` on any violation. Does **not** resolve
    secrets — validation is purely structural.
    """
    if not isinstance(data, dict):
        raise AgentsConfigError(f"{path}: root must be a YAML mapping")

    version = data.get("version")
    if str(version) != SCHEMA_VERSION:
        raise AgentsConfigError(f"{path}: unsupported schema version {version!r} — expected '{SCHEMA_VERSION}'")

    agents = data.get("agents")
    if not isinstance(agents, list):
        raise AgentsConfigError(f"{path}: 'agents' must be a list, got {type(agents).__name__}")

    seen_ids: set[str] = set()
    for i, agent in enumerate(agents):
        if not isinstance(agent, dict):
            raise AgentsConfigError(f"{path}: agents[{i}] must be a mapping")

        agent_id = agent.get("id")
        if not agent_id or not isinstance(agent_id, str):
            raise AgentsConfigError(f"{path}: agents[{i}] is missing the required 'id' field")

        if agent_id in seen_ids:
            raise AgentsConfigError(f"{path}: duplicate agent id '{agent_id}' at agents[{i}]")
        seen_ids.add(agent_id)

        backends = agent.get("backends", {})
        if not isinstance(backends, dict):
            raise AgentsConfigError(f"{path}: agents[{i}] ('{agent_id}').backends must be a mapping")

        slack = backends.get("slack")
        if slack is not None:
            if not isinstance(slack, dict):
                raise AgentsConfigError(f"{path}: agents[{i}] ('{agent_id}').backends.slack must be a mapping")
            for field in ("bot_token", "app_token", "signing_secret"):
                if field not in slack:
                    raise AgentsConfigError(
                        f"{path}: agents[{i}] ('{agent_id}').backends.slack is missing required field '{field}'"
                    )

        discord = backends.get("discord")
        if discord is not None:
            if not isinstance(discord, dict):
                raise AgentsConfigError(f"{path}: agents[{i}] ('{agent_id}').backends.discord must be a mapping")
            if "bot_token" not in discord:
                raise AgentsConfigError(
                    f"{path}: agents[{i}] ('{agent_id}').backends.discord is missing required field 'bot_token'"
                )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_agents_yaml(path: Path | None = None) -> dict[str, Any]:
    """Load, parse, and structurally validate agents.yaml.

    Returns the raw (unresolved) config dict.  Secret placeholders are *not*
    expanded here — call :func:`get_slack_credentials` to get resolved values.

    Raises :class:`AgentsConfigError` for:
    - file not found
    - YAML parse errors
    - any schema violation (wrong version, missing fields, duplicate IDs)
    """
    resolved_path = path if path is not None else DEFAULT_AGENTS_CONFIG_PATH
    try:
        with open(resolved_path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError as exc:
        raise AgentsConfigError(f"agents.yaml not found at {resolved_path}") from exc
    except yaml.YAMLError as exc:
        raise AgentsConfigError(f"Failed to parse {resolved_path}: {exc}") from exc

    _validate_schema(data, resolved_path)
    return data


# ---------------------------------------------------------------------------
# Credential extraction
# ---------------------------------------------------------------------------


def get_slack_credentials(
    agents_config: dict[str, Any],
    resolver: Callable[[str], str | None] | None = None,
) -> dict[str, dict[str, str]]:
    """Resolve and return Slack credentials for all agents in *agents_config*.

    Returns the same shape as :func:`router.config.load_slack_credentials`:
    ``{agent_id: {bot_token, app_token, signing_secret}}``.

    Agents that have no ``backends.slack`` block are silently skipped.
    Agents whose secrets cannot be resolved are skipped with a warning (same
    soft-skip behaviour as :func:`router.config.load_slack_credentials`).
    """
    credentials: dict[str, dict[str, str]] = {}

    for agent in agents_config.get("agents", []):
        agent_id = agent.get("id", "")
        slack_raw = agent.get("backends", {}).get("slack")
        if not slack_raw:
            continue

        try:
            slack = _resolve_obj(slack_raw, resolver)
        except AgentsConfigError as exc:
            logger.warning(
                "Skipping agent '%s' — secret resolution failed: %s",
                agent_id,
                exc,
            )
            continue

        bot_token = slack.get("bot_token", "")
        app_token = slack.get("app_token", "")
        signing_secret = slack.get("signing_secret", "")

        if not (bot_token and app_token and signing_secret):
            missing = [
                name
                for name, val in (
                    ("bot_token", bot_token),
                    ("app_token", app_token),
                    ("signing_secret", signing_secret),
                )
                if not val
            ]
            logger.warning(
                "Skipping agent '%s' — missing Slack credentials after resolution: %s",
                agent_id,
                ", ".join(missing),
            )
            continue

        credentials[agent_id] = {
            "bot_token": bot_token,
            "app_token": app_token,
            "signing_secret": signing_secret,
        }

    return credentials


def load_slack_credentials_from_yaml(
    path: Path | None = None,
    resolver: Callable[[str], str | None] | None = None,
) -> dict[str, dict[str, str]]:
    """Convenience wrapper: load agents.yaml and return resolved Slack credentials.

    Raises :class:`AgentsConfigError` if the file is missing or structurally invalid.
    Individual agents with unresolvable secrets are soft-skipped with a warning.
    """
    agents_config = load_agents_yaml(path)
    return get_slack_credentials(agents_config, resolver)
