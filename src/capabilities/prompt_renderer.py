"""Prompt renderer — generates a human-readable capability summary for the system prompt."""

from __future__ import annotations

from pathlib import Path

from src.capabilities.loader import get_agent_capabilities
from src.capabilities.models import CapabilityInstance

# Ownership-specific notes for delegate/shared accounts, keyed by capability type.
DELEGATE_NOTES: dict[str, str] = {
    "email": "delegate account — no send permission. Create drafts and notify {account} for review.",
    "calendar": "delegate account — booking requires approval.",
}

DELEGATE_WITH_SEND_NOTE = "delegate account — send requires approval."

DEFAULT_DELEGATE_NOTE = "delegate account — high-impact actions require approval."
DEFAULT_SHARED_NOTE = "shared resource — coordinate with other agents."


def render_capability_summary(
    agent_name: str,
    config_path: str | Path | None = None,
) -> str:
    """Render a markdown capability summary for injection into an agent's system prompt.

    Groups instances by capability type, shows account, permissions, ownership context,
    and adds notes for delegate/shared instances.
    """
    agent_caps = get_agent_capabilities(agent_name, config_path)

    lines: list[str] = ["## Your Capabilities", ""]

    for cap_type in sorted(agent_caps.capabilities.keys()):
        instances = agent_caps.capabilities[cap_type]
        lines.append(f"### {cap_type}")

        for inst in instances:
            namespace = f"{cap_type}_{inst.instance}"
            perm_str = ", ".join(inst.permissions)
            lines.append(f"- **{namespace}** ({inst.ownership}) — {inst.account} via {inst.provider}")
            lines.append(f"  Permissions: {perm_str}")

            note = _ownership_note(cap_type, inst)
            if note:
                lines.append(f"  Note: {note}")

        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _ownership_note(cap_type: str, inst: CapabilityInstance) -> str | None:
    """Generate an ownership note for delegate/shared instances."""
    if inst.ownership == "self":
        return None

    if inst.ownership == "delegate":
        # For email, check if send is missing
        if cap_type == "email":
            if "send" not in inst.permissions:
                return DELEGATE_NOTES["email"].replace("{account}", inst.account.split("@")[0])
            return DELEGATE_WITH_SEND_NOTE

        if cap_type in DELEGATE_NOTES:
            return DELEGATE_NOTES[cap_type]

        return DEFAULT_DELEGATE_NOTE

    if inst.ownership == "shared":
        return DEFAULT_SHARED_NOTE

    return None
