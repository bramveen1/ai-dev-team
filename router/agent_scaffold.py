"""Agent scaffolding — pure helpers shared by the add-agent CLI and the /config page.

Moved out of ``scripts/add_agent.py`` (which stays the host-side wizard):
``scripts/`` is not copied into the router image, while the config page's
add-agent endpoint (router/agent_admin.py) needs the same file writers and
Slack app manifest. Dependency direction is unchanged — scripts import from
router, never the reverse.

Everything here is pure (no prompts, no subprocess, no .env edits): build an
:class:`AgentSpec`, write the agent directory files, and produce the
paste-ready Slack app manifest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Agent id = directory name under config/agents/. The regex doubles as the
# path-traversal guard for ids arriving over the config-page API.
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


@dataclass
class AgentSpec:
    id: str
    name: str
    container: str
    thinking_status: str
    role: str
    personality: str
    packs: list[str] = field(default_factory=list)
    scheduled_tasks: list = field(default_factory=list)
    bot_token: str | None = None
    app_token: str | None = None
    signing_secret: str | None = None


# ============================================================================
# Templates
# ============================================================================


def role_template(display_name: str, summary: str) -> str:
    return f"""# {display_name}

{summary}

## Responsibilities

-

## Working Style

-

## Approval Rules

-
"""


def personality_template(display_name: str, blurb: str) -> str:
    return f"""# {display_name} — Personality

{blurb}
"""


# ============================================================================
# File writers
# ============================================================================


def write_agent_files(spec: AgentSpec, agents_dir: Path) -> list[Path]:
    """Create ``<agents_dir>/<id>/`` with agent.yaml, role.md, personality.md.

    Raises :class:`FileExistsError` when the agent directory already exists —
    callers surface this as a conflict rather than overwriting an agent.
    """
    target = agents_dir / spec.id
    target.mkdir(parents=True)

    manifest: dict = {
        "name": spec.name,
        "container": spec.container,
        "thinking_status": spec.thinking_status,
        "packs": list(spec.packs),
    }
    if spec.scheduled_tasks:
        manifest["scheduled_tasks"] = spec.scheduled_tasks

    yaml_path = target / "agent.yaml"
    yaml_path.write_text(yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False, allow_unicode=True))

    role_path = target / "role.md"
    role_path.write_text(spec.role)

    personality_path = target / "personality.md"
    personality_path.write_text(spec.personality)

    return [yaml_path, role_path, personality_path]


def slack_manifest_dict(spec: AgentSpec, slash_prefix: str = "") -> dict:
    """The paste-ready Slack app manifest for ``spec`` as a dict.

    Split from :func:`write_slack_manifest` so the config-page API can return
    the YAML text inline without touching disk.
    """
    slash = f"/{slash_prefix}{spec.id}-tasks"

    return {
        "display_information": {
            "name": spec.name,
            "description": f"{spec.name} — agent",
            "background_color": "#4A154B",
        },
        "features": {
            "bot_user": {"display_name": spec.name, "always_online": True},
            "slash_commands": [
                {
                    "command": slash,
                    "description": "Manage scheduled agent tasks",
                    "usage_hint": "[list | create | pause <id> | resume <id> | delete <id>]",
                    "should_escape": False,
                }
            ],
        },
        "oauth_config": {
            "scopes": {
                "bot": [
                    "app_mentions:read",
                    "channels:history",
                    "channels:read",
                    "chat:write",
                    "commands",
                    "groups:history",
                    "groups:read",
                    "im:history",
                    "im:read",
                    "im:write",
                    "reactions:write",
                    "users:read",
                    "assistant:write",
                ]
            }
        },
        "settings": {
            "event_subscriptions": {
                "bot_events": [
                    "app_mention",
                    "message.channels",
                    "message.groups",
                    "message.im",
                    "assistant_thread_started",
                ]
            },
            "interactivity": {"is_enabled": True},
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


def slack_manifest_yaml(spec: AgentSpec, slash_prefix: str = "") -> str:
    """The Slack app manifest as YAML text (what the operator pastes at api.slack.com/apps)."""
    return yaml.safe_dump(
        slack_manifest_dict(spec, slash_prefix), sort_keys=False, default_flow_style=False, allow_unicode=True
    )


def write_slack_manifest(spec: AgentSpec, output_dir: Path, slash_prefix: str = "") -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{spec.id}.yaml"
    path.write_text(slack_manifest_yaml(spec, slash_prefix))
    return path
