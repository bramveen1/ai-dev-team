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

Then it regenerates ``docker-compose.yml``. ``--apply`` additionally runs
``docker compose up``.

The agent id is the directory name (lowercase, ``^[a-z][a-z0-9_-]*$``). Display
name, container, and slash command default to forms of the id but can be
overridden. Packs (the agent's tool grants) can be pre-selected here or
added later via ``@router grant <agent> <pack>`` from Slack.

Non-interactive YAML schema:

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
    packs:
      - github          # names must match directories under packs/
    scheduled_tasks: []
    slack:
      bot_token: xoxb-...
      app_token: xapp-...
      signing_secret: ...
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml

# Scaffolding primitives live in router/agent_scaffold.py so the /config
# page's add-agent endpoint shares them (scripts/ is not in the router
# image). Re-exported here so existing imports keep working.
from router.agent_scaffold import (
    NAME_RE,
    AgentSpec,
    write_agent_files,
    write_slack_manifest,
)
from router.agent_scaffold import (
    personality_template as _personality_template,
)
from router.agent_scaffold import (
    role_template as _role_template,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_AGENTS_DIR = REPO_ROOT / "config" / "agents"
PACKS_DIR = REPO_ROOT / "packs"
SLACK_MANIFESTS_DIR = REPO_ROOT / "slack-manifests"
ENV_FILE = REPO_ROOT / ".env"

__all__ = [
    "NAME_RE",
    "AgentSpec",
    "write_agent_files",
    "write_slack_manifest",
]


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


def _discover_pack_names(packs_dir: Path) -> list[str]:
    """Return sorted pack directory names that contain a ``pack.yaml``.

    Skips ``_template`` and any other ``_``-prefixed directories. Avoids
    importing ``router.packs.loader`` so the wizard stays usable without
    the runtime dependencies installed.
    """
    if not packs_dir.exists():
        return []
    names: list[str] = []
    for entry in sorted(packs_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        if (entry / "pack.yaml").exists():
            names.append(entry.name)
    return names


def _prompt_packs(packs_dir: Path) -> list[str]:
    """Multi-select prompt: pick which packs to grant up-front.

    Returns a list of pack names (possibly empty). The grant flow does not
    run here — it just records the names in ``agent.yaml``. Granting the
    secrets / running ``authenticate.py`` happens later via Slack.
    """
    available = _discover_pack_names(packs_dir)
    if not available:
        print("\n(No packs found under packs/ — skipping pack prompt.)")
        return []

    print("\nAvailable packs:")
    for i, name in enumerate(available, 1):
        print(f"  {i}. {name}")
    print("  (You can also add packs later via `@router grant <agent> <pack>` in Slack.)")
    raw = _prompt("Pick packs (comma-separated numbers, or names; blank to skip)")
    if not raw:
        return []

    chosen: list[str] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < len(available):
                chosen.append(available[idx])
            else:
                print(f"  ignoring out-of-range index: {token}", file=sys.stderr)
        elif token in available:
            chosen.append(token)
        else:
            print(f"  ignoring unknown pack: {token!r}", file=sys.stderr)

    # De-duplicate while preserving order.
    seen: set[str] = set()
    return [p for p in chosen if not (p in seen or seen.add(p))]


def prompt_for_spec(agents_dir: Path, packs_dir: Path, no_slack: bool) -> AgentSpec:
    print("\nadd-agent wizard\n")
    agent_id = _prompt_id(agents_dir)
    display_name = _prompt("Display name", default=agent_id.capitalize())
    container = _prompt("Container name", default=agent_id)
    thinking_status = _prompt("Thinking status (shown in Slack)", default="is thinking through this…")

    role_summary = _prompt("One-line role description (becomes role.md)", required=True)
    personality_blurb = _prompt("Personality blurb (becomes personality.md)", required=True)

    packs = _prompt_packs(packs_dir)

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
        packs=packs,
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

    raw_packs = data.get("packs") or []
    if not isinstance(raw_packs, list):
        raise ValueError(f"{path}: 'packs' must be a list of pack names")
    packs = [str(p) for p in raw_packs]

    return AgentSpec(
        id=agent_id,
        name=display_name,
        container=container,
        thinking_status=data.get("thinking_status", "is thinking…"),
        role=role,
        personality=personality,
        packs=packs,
        scheduled_tasks=data.get("scheduled_tasks") or [],
        bot_token=slack.get("bot_token"),
        app_token=slack.get("app_token"),
        signing_secret=slack.get("signing_secret"),
    )


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
# Main
# ============================================================================


def _print_summary(spec: AgentSpec) -> None:
    print("\n=== Summary ===")
    print(f"  id              {spec.id}")
    print(f"  display name    {spec.name}")
    print(f"  container       {spec.container}")
    if spec.packs:
        print(f"  packs           {', '.join(spec.packs)}")
        print("                  (run `@router grant <agent> <pack>` in Slack to provision their secrets)")
    else:
        print("  packs           (none — grant via `@router grant <agent> <pack>` from Slack)")
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
    if spec.packs:
        joined = ", ".join(spec.packs)
        print(f"  3. From Slack, run `@router grant {spec.id} <pack>` for each of: {joined}")
        print(f"  4. DM @{spec.name} in Slack to confirm.")
    else:
        print(f"  3. DM @{spec.name} in Slack to confirm.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a new agent.")
    parser.add_argument("--from-yaml", type=Path, dest="from_yaml", help="non-interactive — load spec from YAML")
    parser.add_argument("--no-slack", action="store_true", help="skip Slack token prompts")
    parser.add_argument("--apply", action="store_true", help="run docker compose up after writing")
    parser.add_argument("--slash-prefix", default="", help="prefix for slash command names (e.g. 'dev-')")
    parser.add_argument("--agents-dir", type=Path, default=CONFIG_AGENTS_DIR)
    parser.add_argument("--packs-dir", type=Path, default=PACKS_DIR)
    parser.add_argument("--slack-manifest-dir", type=Path, default=SLACK_MANIFESTS_DIR)
    parser.add_argument("--env-file", type=Path, default=ENV_FILE)
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="skip the 'Proceed?' prompt (implicit when --from-yaml is set)",
    )
    parser.add_argument("--no-render-compose", action="store_true", help="skip regenerating docker-compose.yml")
    args = parser.parse_args(argv)

    try:
        if args.from_yaml:
            spec = load_spec_from_yaml(args.from_yaml, args.no_slack)
        else:
            spec = prompt_for_spec(args.agents_dir, args.packs_dir, args.no_slack)
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
