"""Wizard for adding a new agent.

Two modes:

    python -m scripts.add_agent                      # interactive
    python -m scripts.add_agent --from-yaml spec.yml # non-interactive

The wizard writes:

    config/agents/<id>/agent.yaml      — manifest (auto-discovered by router)
    config/agents/<id>/role.md         — role description
    config/agents/<id>/personality.md  — voice/style notes
    slack-manifests/<id>.yaml          — paste this at api.slack.com/apps
    .env                               — appends bot/app/signing token vars

Then it regenerates ``docker-compose.yml`` and runs the capabilities loader to
validate the new manifest. ``--apply`` additionally runs ``docker compose up``.

The agent id is the directory name (lowercase, ``^[a-z][a-z0-9_-]*$``). Display
name, container, and slash command default to forms of the id but can be
overridden.

Non-interactive YAML schema (see tests/fixtures/maya.yaml):

    id: maya
    name: Maya
    container: maya
    thinking_status: "is sketching…"
    role: |
      # Maya — Designer
      ...
    personality: |
      # Maya — Personality
      ...
    capabilities:
      email:
        - instance: mine
          provider: gmail-connector
          account: maya@pathtohired.com
          ownership: self
          permissions: [read, send]
    scheduled_tasks: []
    slack:
      bot_token: xoxb-...
      app_token: xapp-...
      signing_secret: ...
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_AGENTS_DIR = REPO_ROOT / "config" / "agents"
SLACK_MANIFESTS_DIR = REPO_ROOT / "slack-manifests"
ENV_FILE = REPO_ROOT / ".env"
PROVIDERS_YAML = REPO_ROOT / "config" / "providers.yaml"

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
BASELINE_CAPABILITIES = {"web", "memory", "slack_io", "scheduled_tasks", "scheduled-tasks"}


@dataclass
class AgentSpec:
    id: str
    name: str
    container: str
    thinking_status: str
    role: str
    personality: str
    capabilities: dict = field(default_factory=dict)
    scheduled_tasks: list = field(default_factory=list)
    bot_token: str | None = None
    app_token: str | None = None
    signing_secret: str | None = None


# ============================================================================
# Interactive prompts
# ============================================================================


def _prompt(label: str, default: str = "", required: bool = False) -> str:
    suffix = f" [{default}]: " if default else ": "
    while True:
        try:
            value = input(f"{label}{suffix}").strip()
        except EOFError:
            return default
        if not value:
            value = default
        if required and not value:
            print("  required.", file=sys.stderr)
            continue
        return value


def _prompt_yes_no(label: str, default: bool = False) -> bool:
    default_str = "Y/n" if default else "y/N"
    while True:
        try:
            value = input(f"{label} [{default_str}]: ").strip().lower()
        except EOFError:
            return default
        if not value:
            return default
        if value in ("y", "yes"):
            return True
        if value in ("n", "no"):
            return False
        print("  please enter y or n", file=sys.stderr)


def _prompt_id(agents_dir: Path) -> str:
    while True:
        agent_id = _prompt("Agent id (lowercase, no spaces)", required=True)
        if not NAME_RE.match(agent_id):
            print(f"  invalid: must match {NAME_RE.pattern!r}", file=sys.stderr)
            continue
        if (agents_dir / agent_id).exists():
            print(f"  conflict: {agents_dir / agent_id} already exists", file=sys.stderr)
            continue
        return agent_id


def _load_providers_by_capability(providers_path: Path) -> dict[str, list[str]]:
    if not providers_path.exists():
        return {}
    with open(providers_path) as f:
        data = yaml.safe_load(f) or {}
    by_cap: dict[str, list[str]] = {}
    for name, cfg in (data.get("providers") or {}).items():
        for cap in cfg.get("capabilities", []) or []:
            by_cap.setdefault(cap, []).append(name)
    return by_cap


def _prompt_capabilities(providers_path: Path) -> dict:
    by_cap = _load_providers_by_capability(providers_path)
    selectable = sorted(c for c in by_cap if c not in BASELINE_CAPABILITIES)
    if not selectable:
        return {}

    print("\nAvailable capabilities (baseline ones — web, memory, slack_io, scheduled_tasks — auto-merge):")
    for i, cap in enumerate(selectable, 1):
        print(f"  {i}. {cap}")
    raw = _prompt("Pick capability numbers (comma-separated, blank to skip)")
    if not raw:
        return {}

    try:
        indices = [int(x.strip()) - 1 for x in raw.split(",") if x.strip()]
    except ValueError:
        print("  invalid input — skipping capabilities", file=sys.stderr)
        return {}

    capabilities: dict = {}
    for i in indices:
        if 0 <= i < len(selectable):
            cap = selectable[i]
            instance = _prompt_one_instance(cap, by_cap[cap])
            if instance is not None:
                capabilities[cap] = [instance]
    return capabilities


def _prompt_one_instance(cap_type: str, providers: list[str]) -> dict | None:
    print(f"\n  Configuring '{cap_type}':")
    for i, p in enumerate(providers, 1):
        print(f"    {i}. {p}")
    while True:
        raw = _prompt(f"    Pick provider [1-{len(providers)}]")
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(providers):
                provider = providers[idx]
                break
        except ValueError:
            pass
        print("    invalid choice", file=sys.stderr)

    instance = _prompt("    Instance name (e.g. 'mine', 'bram')", default="mine")
    account = _prompt("    Account (email or id)", required=True)
    ownership = _prompt("    Ownership (self|delegate|shared)", default="self")

    valid_perms = sorted(_permission_vocabulary().get(cap_type, set()))
    print(f"    Available permissions: {', '.join(valid_perms) or '(none)'}")
    perms_raw = _prompt("    Permissions (comma-separated)", default=",".join(valid_perms))
    permissions = [p.strip() for p in perms_raw.split(",") if p.strip()]

    return {
        "instance": instance,
        "provider": provider,
        "account": account,
        "ownership": ownership,
        "permissions": permissions,
    }


def _permission_vocabulary() -> dict[str, set[str]]:
    """Lazy import — keeps the wizard usable even if pydantic isn't installed."""
    try:
        from capabilities.models import PERMISSION_VOCABULARY

        return PERMISSION_VOCABULARY
    except ImportError:
        return {}


def prompt_for_spec(agents_dir: Path, providers_path: Path, no_slack: bool) -> AgentSpec:
    print("\nadd-agent wizard\n")
    agent_id = _prompt_id(agents_dir)
    display_name = _prompt("Display name", default=agent_id.capitalize())
    container = _prompt("Container name", default=agent_id)
    thinking_status = _prompt("Thinking status (shown in Slack)", default="is thinking through this…")

    role_summary = _prompt("One-line role description (becomes role.md)", required=True)
    personality_blurb = _prompt("Personality blurb (becomes personality.md)", required=True)

    capabilities = _prompt_capabilities(providers_path)

    if no_slack:
        bot_token = app_token = signing_secret = None
    else:
        print("\nSlack tokens (press Enter to skip if not ready — placeholders go in .env):")
        bot_token = _prompt("  Bot token (xoxb-...)") or None
        app_token = _prompt("  App token (xapp-...)") or None
        signing_secret = _prompt("  Signing secret") or None

    return AgentSpec(
        id=agent_id,
        name=display_name,
        container=container,
        thinking_status=thinking_status,
        role=_role_template(display_name, role_summary),
        personality=_personality_template(display_name, personality_blurb),
        capabilities=capabilities,
        bot_token=bot_token,
        app_token=app_token,
        signing_secret=signing_secret,
    )


# ============================================================================
# Non-interactive (--from-yaml)
# ============================================================================


def load_spec_from_yaml(path: Path, no_slack: bool) -> AgentSpec:
    with open(path) as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a YAML mapping")

    agent_id = data.get("id") or path.stem
    if not NAME_RE.match(agent_id):
        raise ValueError(f"invalid agent id: {agent_id!r}")

    display_name = data.get("name") or agent_id.capitalize()
    container = data.get("container") or agent_id
    role = data.get("role") or _role_template(display_name, "Role description placeholder.")
    personality = data.get("personality") or _personality_template(display_name, "Personality placeholder.")

    slack = (data.get("slack") or {}) if not no_slack else {}

    return AgentSpec(
        id=agent_id,
        name=display_name,
        container=container,
        thinking_status=data.get("thinking_status", "is thinking…"),
        role=role,
        personality=personality,
        capabilities=data.get("capabilities") or {},
        scheduled_tasks=data.get("scheduled_tasks") or [],
        bot_token=slack.get("bot_token"),
        app_token=slack.get("app_token"),
        signing_secret=slack.get("signing_secret"),
    )


# ============================================================================
# Templates
# ============================================================================


def _role_template(display_name: str, summary: str) -> str:
    return f"""# {display_name}

{summary}

## Responsibilities

-

## Working Style

-

## Approval Rules

-
"""


def _personality_template(display_name: str, blurb: str) -> str:
    return f"""# {display_name} — Personality

{blurb}
"""


# ============================================================================
# File writers
# ============================================================================


def write_agent_files(spec: AgentSpec, agents_dir: Path) -> list[Path]:
    target = agents_dir / spec.id
    target.mkdir(parents=True)

    manifest: dict = {
        "name": spec.name,
        "container": spec.container,
        "thinking_status": spec.thinking_status,
        "capabilities": spec.capabilities or {},
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


def write_slack_manifest(spec: AgentSpec, output_dir: Path, slash_prefix: str = "") -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{spec.id}.yaml"

    slash = f"/{slash_prefix}{spec.id}-tasks"

    manifest = {
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

    path.write_text(yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False, allow_unicode=True))
    return path


def append_env(spec: AgentSpec, env_file: Path) -> bool:
    """Append the agent's three Slack vars to ``env_file`` (real values or placeholders).

    Returns True if anything was appended; False if the agent's vars were
    already present.
    """
    prefix = spec.id.upper()
    bot = spec.bot_token or "xoxb-..."
    app = spec.app_token or "xapp-..."
    secret = spec.signing_secret or "..."

    block = f"\n# {spec.name}\n{prefix}_BOT_TOKEN={bot}\n{prefix}_APP_TOKEN={app}\n{prefix}_SIGNING_SECRET={secret}\n"

    if env_file.exists():
        existing = env_file.read_text()
        if f"{prefix}_BOT_TOKEN=" in existing:
            return False
        env_file.write_text(existing.rstrip() + block)
    else:
        env_file.write_text("# Bot tokens for the router\n" + block.lstrip())
    return True


# ============================================================================
# Validation
# ============================================================================


def validate_spec(agent_id: str) -> tuple[bool, str]:
    """Run the capabilities loader against the new agent. Returns (ok, message)."""
    try:
        from capabilities.loader import get_agent_capabilities

        caps = get_agent_capabilities(agent_id)
    except Exception as e:
        return False, str(e)
    count = sum(len(v) for v in caps.capabilities.values())
    return True, f"loaded {count} capability instance(s)"


# ============================================================================
# Main
# ============================================================================


def _print_summary(spec: AgentSpec) -> None:
    print("\n=== Summary ===")
    print(f"  id              {spec.id}")
    print(f"  display name    {spec.name}")
    print(f"  container       {spec.container}")
    print(f"  capabilities    {list(spec.capabilities.keys()) or '(baseline only)'}")
    print(f"  files           config/agents/{spec.id}/{{agent.yaml, role.md, personality.md}}")
    print(f"                  slack-manifests/{spec.id}.yaml")
    have_real = bool(spec.bot_token or spec.app_token or spec.signing_secret)
    label = "real values" if have_real else "placeholders — fill them in after Slack-app creation"
    print(f"  .env            append {spec.id.upper()}_*_TOKEN/SIGNING_SECRET ({label})")


def _print_next_steps(spec: AgentSpec, slack_path: Path) -> None:
    try:
        slack_display = slack_path.relative_to(REPO_ROOT)
    except ValueError:
        slack_display = slack_path
    print("\n=== Next steps ===")
    print(f"  1. Create the Slack app — paste {slack_display} at https://api.slack.com/apps")
    if not (spec.bot_token and spec.app_token and spec.signing_secret):
        print("     Then copy the 3 tokens into .env (replace the placeholders).")
    print(f"  2. make up   # rebuilds compose and brings up the {spec.id} container")
    print(f"  3. DM @{spec.name} in Slack to confirm.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a new agent.")
    parser.add_argument("--from-yaml", type=Path, dest="from_yaml", help="non-interactive — load spec from YAML")
    parser.add_argument("--no-slack", action="store_true", help="skip Slack token prompts")
    parser.add_argument("--apply", action="store_true", help="run docker compose up after writing")
    parser.add_argument("--slash-prefix", default="", help="prefix for slash command names (e.g. 'dev-')")
    parser.add_argument("--agents-dir", type=Path, default=CONFIG_AGENTS_DIR)
    parser.add_argument("--slack-manifest-dir", type=Path, default=SLACK_MANIFESTS_DIR)
    parser.add_argument("--env-file", type=Path, default=ENV_FILE)
    parser.add_argument("--providers-yaml", type=Path, default=PROVIDERS_YAML)
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="skip the 'Proceed?' prompt (implicit when --from-yaml is set)",
    )
    parser.add_argument("--no-render-compose", action="store_true", help="skip regenerating docker-compose.yml")
    parser.add_argument("--no-validate", action="store_true", help="skip the capabilities-render validation")
    args = parser.parse_args(argv)

    try:
        if args.from_yaml:
            spec = load_spec_from_yaml(args.from_yaml, args.no_slack)
        else:
            spec = prompt_for_spec(args.agents_dir, args.providers_yaml, args.no_slack)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    target = args.agents_dir / spec.id
    if target.exists():
        print(f"error: {target} already exists. Pick a different id or remove the directory.", file=sys.stderr)
        return 1

    _print_summary(spec)
    if not args.no_confirm and not args.from_yaml:
        if not _prompt_yes_no("\nProceed?", default=True):
            print("aborted")
            return 0

    written = write_agent_files(spec, args.agents_dir)
    slack_path = write_slack_manifest(spec, args.slack_manifest_dir, slash_prefix=args.slash_prefix)
    env_appended = append_env(spec, args.env_file)

    print(f"\nwrote {len(written)} agent files")
    print(f"wrote {slack_path}")
    if env_appended:
        print(f"appended tokens to {args.env_file}")
    else:
        print(f"{args.env_file} already has {spec.id.upper()}_*_TOKEN — left it alone")

    if not args.no_render_compose:
        from scripts.render_compose import main as render_main

        render_main(["--agents-dir", str(args.agents_dir)])

    if not args.no_validate and args.agents_dir == CONFIG_AGENTS_DIR:
        ok, msg = validate_spec(spec.id)
        prefix = "ok" if ok else "WARNING"
        print(f"\nvalidation: {prefix} — {msg}")
        if not ok:
            print(f"Edit {target}/agent.yaml to fix.", file=sys.stderr)

    _print_next_steps(spec, slack_path)

    if args.apply:
        subprocess.run(
            ["docker", "compose", "up", "--build", "-d", spec.id],
            cwd=str(REPO_ROOT),
            check=False,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
