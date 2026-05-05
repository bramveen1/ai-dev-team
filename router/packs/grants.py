"""Slack-driven pack provisioning — ``grant`` / ``revoke`` / ``list packs`` / ``who has``.

These commands let a non-technical user grant pack access from Slack without
touching Dockerfiles, ``.env``, or the terminal. The flow is documented in
``docs/capabilities-simplification.md`` (issue #83 / PR 3).

Command shapes (case-insensitive, leading ``@<bot>`` mention is stripped):

```
grant <agent> <pack>
revoke <agent> <pack>
list packs
who has <pack>
```

The handlers are agent-agnostic — any agent's Slack app can receive these
commands; the target agent is named in the command text. So you can DM Lisa
``grant sam github`` and Lisa's app handles it.

This module mutates two pieces of state:

- ``data/secrets.json`` (via :class:`router.packs.secret_store.SecretStore`)
  for any tokens returned by the pack's ``authenticate.py``.
- ``config/agents/<agent>/agent.yaml`` to append/remove the pack name from
  the ``packs:`` list. We use targeted regex edits rather than a full
  PyYAML round-trip so existing comments and formatting survive.
"""

from __future__ import annotations

import importlib.util
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

from router.packs.loader import REPO_ROOT, Pack, discover_packs
from router.packs.secret_store import SecretStore

logger = logging.getLogger(__name__)

DEFAULT_AGENTS_DIR = REPO_ROOT / "config" / "agents"

SayCallable = Callable[[str], Awaitable[Any]]

# Command regexes. ``\w`` matches word chars; pack names also allow hyphens.
_MENTION_PREFIX_RE = re.compile(r"^<@[A-Z0-9]+>\s*")
_GRANT_RE = re.compile(r"^grant\s+(?P<agent>[a-z0-9_-]+)\s+(?P<pack>[a-z0-9_-]+)\s*$", re.IGNORECASE)
_REVOKE_RE = re.compile(r"^revoke\s+(?P<agent>[a-z0-9_-]+)\s+(?P<pack>[a-z0-9_-]+)\s*$", re.IGNORECASE)
_LIST_PACKS_RE = re.compile(r"^list\s+packs\s*$", re.IGNORECASE)
_WHO_HAS_RE = re.compile(r"^who\s+has\s+(?P<pack>[a-z0-9_-]+)\s*$", re.IGNORECASE)


@dataclass
class GrantCommand:
    agent: str
    pack: str


@dataclass
class RevokeCommand:
    agent: str
    pack: str


@dataclass
class ListPacksCommand:
    pass


@dataclass
class WhoHasCommand:
    pack: str


PackCommand = GrantCommand | RevokeCommand | ListPacksCommand | WhoHasCommand


def parse_command(text: str) -> PackCommand | None:
    """Parse a Slack message into a pack command (or ``None`` if not one)."""
    cleaned = _MENTION_PREFIX_RE.sub("", (text or "").strip())
    if not cleaned:
        return None
    if m := _GRANT_RE.match(cleaned):
        return GrantCommand(agent=m["agent"].lower(), pack=m["pack"].lower())
    if m := _REVOKE_RE.match(cleaned):
        return RevokeCommand(agent=m["agent"].lower(), pack=m["pack"].lower())
    if _LIST_PACKS_RE.match(cleaned):
        return ListPacksCommand()
    if m := _WHO_HAS_RE.match(cleaned):
        return WhoHasCommand(pack=m["pack"].lower())
    return None


# ── Handlers ─────────────────────────────────────────────────────────


async def handle_grant(
    cmd: GrantCommand,
    say: SayCallable,
    *,
    packs_dir: Path | None = None,
    agents_dir: Path | None = None,
    secret_store: SecretStore | None = None,
) -> None:
    """Run the grant flow for one ``(agent, pack)`` pair."""
    packs = discover_packs(packs_dir)
    if cmd.pack not in packs:
        await say(f":x: Pack `{cmd.pack}` not found. Try `list packs` to see what's available.")
        return
    pack = packs[cmd.pack]

    agent_yaml = _agent_manifest_path(cmd.agent, agents_dir)
    if not agent_yaml.exists():
        await say(f":x: Agent `{cmd.agent}` not found.")
        return

    if cmd.pack in _read_packs_list(agent_yaml):
        await say(f":information_source: `{cmd.agent}` already has `{cmd.pack}`.")
        return

    store = secret_store or SecretStore()
    if pack.authenticate_path is not None:
        await say(f"Setting up `{cmd.pack}` for `{cmd.agent}`…")
        try:
            secrets = await _run_authenticate(pack, say)
        except Exception as e:
            logger.exception("authenticate.py failed for pack %s", pack.name)
            await say(f":x: Setup for `{cmd.pack}` failed: {e}")
            return
        if secrets:
            store.set(pack.name, secrets)
    elif pack.needs:
        await say(
            f":warning: Pack `{cmd.pack}` declares `needs: {pack.needs}` but ships no "
            f"`authenticate.py`. Add the values manually to `data/secrets.json` "
            f'under `"{pack.name}"`, then re-run grant.'
        )
        return

    _append_pack_to_manifest(agent_yaml, cmd.pack)
    await say(
        f":white_check_mark: Granted `{cmd.pack}` to `{cmd.agent}`. "
        f"Run `docker compose restart {cmd.agent}` to pick up the change."
    )


async def handle_revoke(
    cmd: RevokeCommand,
    say: SayCallable,
    *,
    agents_dir: Path | None = None,
    secret_store: SecretStore | None = None,
    drop_secret: bool = False,
) -> None:
    """Remove a pack from an agent's manifest. Optionally drop its secret block."""
    agent_yaml = _agent_manifest_path(cmd.agent, agents_dir)
    if not agent_yaml.exists():
        await say(f":x: Agent `{cmd.agent}` not found.")
        return

    current = _read_packs_list(agent_yaml)
    if cmd.pack not in current:
        await say(f":information_source: `{cmd.agent}` doesn't have `{cmd.pack}`.")
        return

    _remove_pack_from_manifest(agent_yaml, cmd.pack)

    extra = ""
    if drop_secret:
        store = secret_store or SecretStore()
        if store.delete(cmd.pack):
            extra = " Secret block deleted."

    await say(
        f":white_check_mark: Revoked `{cmd.pack}` from `{cmd.agent}`.{extra} "
        f"Run `docker compose restart {cmd.agent}` to pick up the change."
    )


async def handle_list_packs(say: SayCallable, *, packs_dir: Path | None = None) -> None:
    """List every available pack with a one-line description."""
    packs = discover_packs(packs_dir)
    if not packs:
        await say("No packs available. Authoring guide: `docs/authoring-a-pack.md`.")
        return
    lines = [f"*Available packs ({len(packs)}):*"]
    for name, pack in sorted(packs.items()):
        desc = (pack.description or "").splitlines()[0] if pack.description else "(no description)"
        lines.append(f"• `{name}` — {desc}")
    await say("\n".join(lines))


async def handle_who_has(
    cmd: WhoHasCommand,
    say: SayCallable,
    *,
    agents_dir: Path | None = None,
) -> None:
    """Show every agent whose manifest lists ``cmd.pack``."""
    base = agents_dir if agents_dir is not None else DEFAULT_AGENTS_DIR
    holders: list[str] = []
    if base.exists():
        for agent_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            yaml_path = agent_dir / "agent.yaml"
            if yaml_path.exists() and cmd.pack in _read_packs_list(yaml_path):
                holders.append(agent_dir.name)
    if not holders:
        await say(f"No agent has `{cmd.pack}`.")
        return
    await say(f"Agents with `{cmd.pack}`: {', '.join(f'`{h}`' for h in holders)}.")


async def maybe_handle_pack_command(
    text: str,
    say: SayCallable,
    *,
    packs_dir: Path | None = None,
    agents_dir: Path | None = None,
    secret_store: SecretStore | None = None,
) -> bool:
    """Detect and handle a pack command in ``text``. Return True if handled."""
    cmd = parse_command(text)
    if cmd is None:
        return False
    if isinstance(cmd, GrantCommand):
        await handle_grant(cmd, say, packs_dir=packs_dir, agents_dir=agents_dir, secret_store=secret_store)
    elif isinstance(cmd, RevokeCommand):
        await handle_revoke(cmd, say, agents_dir=agents_dir, secret_store=secret_store)
    elif isinstance(cmd, ListPacksCommand):
        await handle_list_packs(say, packs_dir=packs_dir)
    elif isinstance(cmd, WhoHasCommand):
        await handle_who_has(cmd, say, agents_dir=agents_dir)
    return True


# ── Internal helpers ─────────────────────────────────────────────────


def _agent_manifest_path(agent: str, agents_dir: Path | None) -> Path:
    base = agents_dir if agents_dir is not None else DEFAULT_AGENTS_DIR
    return base / agent / "agent.yaml"


async def _run_authenticate(pack: Pack, say: SayCallable) -> dict[str, Any]:
    """Dynamically import and call ``pack/authenticate.py::acquire(say)``."""
    spec = importlib.util.spec_from_file_location(
        f"_packs_runtime.{pack.name}.authenticate",
        pack.authenticate_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {pack.authenticate_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "acquire"):
        raise RuntimeError(f"{pack.authenticate_path} is missing `async def acquire(say)`")
    return await module.acquire(say)


def _read_packs_list(agent_yaml: Path) -> list[str]:
    with open(agent_yaml) as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        return []
    value = raw.get("packs") or []
    return list(value) if isinstance(value, list) else []


# Match an existing top-level ``packs:`` block including its body (lines that
# are blank or indented), terminated by the next top-level key or EOF. Used by
# both append and remove so the surrounding manifest stays untouched.
_PACKS_BLOCK_RE = re.compile(
    r"(?m)^packs\s*:(?:[^\n]*)(?:\n[ \t][^\n]*|\n[ \t]*)*",
)


def _render_packs_block(packs: list[str]) -> str:
    if not packs:
        return "packs: []"
    return "packs:\n" + "\n".join(f"  - {p}" for p in sorted(packs))


def _replace_or_append_packs(text: str, new_block: str) -> str:
    if _PACKS_BLOCK_RE.search(text):
        return _PACKS_BLOCK_RE.sub(new_block, text, count=1)
    if not text.endswith("\n"):
        text += "\n"
    return text + "\n" + new_block + "\n"


def _atomic_write_validated(path: Path, new_text: str) -> None:
    """Write the manifest only if it round-trips through PyYAML cleanly."""
    try:
        yaml.safe_load(new_text)
    except yaml.YAMLError as e:
        raise RuntimeError(f"refusing to write malformed manifest {path}: {e}") from e
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_text)
    tmp.replace(path)


def _append_pack_to_manifest(agent_yaml: Path, pack_name: str) -> None:
    text = agent_yaml.read_text()
    existing = _read_packs_list(agent_yaml)
    if pack_name in existing:
        return
    new_packs = sorted(set(existing) | {pack_name})
    updated = _replace_or_append_packs(text, _render_packs_block(new_packs))
    _atomic_write_validated(agent_yaml, updated)


def _remove_pack_from_manifest(agent_yaml: Path, pack_name: str) -> None:
    text = agent_yaml.read_text()
    existing = _read_packs_list(agent_yaml)
    if pack_name not in existing:
        return
    new_packs = sorted(p for p in existing if p != pack_name)
    updated = _replace_or_append_packs(text, _render_packs_block(new_packs))
    _atomic_write_validated(agent_yaml, updated)
