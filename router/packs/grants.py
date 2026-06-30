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

import asyncio
import importlib.util
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

from router.packs import browser_credentials
from router.packs.loader import Pack, discover_packs
from router.packs.secret_store import SecretStore

logger = logging.getLogger(__name__)

# Read/write agent manifests against the live ``./config`` bind-mount
# (``/config`` inside the router container), NOT ``/app/config`` which is
# baked into the router image and goes stale after any gitignored
# ``agent.yaml`` edit. Keeps ``grant``/``revoke`` writes consistent with
# ``dispatch_hook``'s reads.
DEFAULT_AGENTS_DIR = Path("/config/agents")
DEFAULT_PROMPT_TIMEOUT_SECONDS = 300

SayCallable = Callable[[str], Awaitable[Any]]


# Pending reply futures keyed by (channel, thread_ts). Set by SlackPrompt.prompt
# when an authenticate.py awaits the user's next message; resolved by
# resolve_pending_reply when that message arrives.
_pending_replies: dict[tuple[str, str], asyncio.Future[str]] = {}


class SlackPrompt:
    """A ``say``-callable that also supports waiting for the user's next reply.

    Pack authenticate.py modules receive one of these instead of a raw say.
    Calling ``await prompt("question")`` posts to Slack and waits for the
    user's next message in the same thread; ``await prompt(text)`` is the
    same as the legacy ``await say(text)`` — post and continue.

    When the prompt is constructed without ``channel``/``thread_ts`` (e.g. in
    unit tests), :meth:`prompt` raises rather than silently hanging. Tests
    can subclass and override.
    """

    def __init__(
        self,
        say: SayCallable,
        *,
        channel: str | None = None,
        thread_ts: str | None = None,
    ) -> None:
        self._say = say
        self.channel = channel
        self.thread_ts = thread_ts

    async def __call__(self, text: str) -> None:
        await self._say(text)

    async def prompt(
        self,
        text: str,
        *,
        timeout: float = DEFAULT_PROMPT_TIMEOUT_SECONDS,
    ) -> str:
        """Post ``text`` and await the user's next reply in this thread."""
        if self.channel is None or self.thread_ts is None:
            raise RuntimeError(
                "SlackPrompt.prompt() requires channel and thread_ts; this prompt was constructed without them."
            )
        await self._say(text)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        key = (self.channel, self.thread_ts)
        _pending_replies[key] = future
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            _pending_replies.pop(key, None)


def resolve_pending_reply(channel: str, thread_ts: str, text: str) -> bool:
    """If a pack authenticate flow is waiting on this thread, deliver ``text``.

    Returns True if the reply was consumed (caller should NOT continue normal
    dispatch), False otherwise.
    """
    future = _pending_replies.get((channel, thread_ts))
    if future is None or future.done():
        return False
    future.set_result(text)
    return True


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
    channel: str | None = None,
    thread_ts: str | None = None,
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

    store = secret_store or SecretStore()
    already_in_manifest = cmd.pack in _read_packs_list(agent_yaml)
    secret_present = bool(store.get(pack.name)) if pack.needs else True

    # Idempotent: only short-circuit when *both* the manifest and the secret
    # store are fully provisioned. If the pack is in the manifest but the
    # secret is missing (e.g. seeded by a PR before the grant was run, or the
    # block was deleted to rotate), still run the authenticate flow so we can
    # recover instead of leaving the agent stuck without env vars.
    if already_in_manifest and secret_present:
        await say(f":information_source: `{cmd.agent}` already has `{cmd.pack}`.")
        return

    prompt = SlackPrompt(say, channel=channel, thread_ts=thread_ts)
    if pack.authenticate_path is not None:
        await say(f"Setting up `{cmd.pack}` for `{cmd.agent}`…")
        try:
            secrets = await _run_authenticate(pack, prompt)
        except Exception as e:
            logger.exception("authenticate.py failed for pack %s", pack.name)
            await say(f":x: Setup for `{cmd.pack}` failed: {e}")
            return
        if secrets:
            store.set(pack.name, secrets)
    elif pack.needs and not secret_present:
        await say(
            f":warning: Pack `{cmd.pack}` declares `needs: {pack.needs}` but ships no "
            f"`authenticate.py`. Add the values manually to `data/secrets.json` "
            f'under `"{pack.name}"`, then re-run grant.'
        )
        return

    if pack.install_path is not None:
        await say(f"Running `{pack.name}/install.sh` to provision the CLI…")
        try:
            await _run_install(pack)
        except Exception as e:
            logger.exception("install.sh failed for pack %s", pack.name)
            await say(f":x: `{pack.name}/install.sh` failed: {e}")
            return

    if not already_in_manifest:
        _append_pack_to_manifest(agent_yaml, cmd.pack)

    if already_in_manifest:
        suffix = "Restored the missing token; the manifest entry was already present."
    else:
        suffix = f"Run `docker compose restart {cmd.agent}` to pick up the change."
    await say(f":white_check_mark: Granted `{cmd.pack}` to `{cmd.agent}`. {suffix}")


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


_PACK_VERBS: frozenset[str] = frozenset({"grant", "revoke", "list packs", "who has"})


async def maybe_handle_pack_command(
    text: str,
    say: SayCallable,
    *,
    packs_dir: Path | None = None,
    agents_dir: Path | None = None,
    secret_store: SecretStore | None = None,
    channel: str | None = None,
    thread_ts: str | None = None,
    sidecar_url: str | None = None,
) -> bool:
    """Detect and handle a pack command in ``text``. Return True if handled.

    The credential subcommands (``grant <profile> credentials <key>``
    and the matching revoke) are checked **before** the grammar router so
    the more-specific credential shape is not mis-read as ``grant <agent>
    <pack>``.

    Pack verb detection now routes through ``router.commands.grammar`` (the
    transport-neutral command grammar).  The grammar identifies the verb;
    structured arg parsing for grant/revoke uses the existing
    :func:`parse_command` helper for backward compatibility with the
    two-positional ``grant <agent> <pack>`` form.
    """
    from router.commands.slack_shim import parse_from_message

    # Issue #147 — Slack-grant flow for browser_use credentials.
    cred_cmd = browser_credentials.parse_credential_command(text)
    if cred_cmd is not None:
        prompt = SlackPrompt(say, channel=channel, thread_ts=thread_ts)
        if isinstance(cred_cmd, browser_credentials.CredentialGrantCommand):
            await browser_credentials.handle_credential_grant(
                cred_cmd, prompt, channel=channel, sidecar_url=sidecar_url
            )
        else:
            await browser_credentials.handle_credential_revoke(
                cred_cmd, prompt, channel=channel, sidecar_url=sidecar_url
            )
        return True

    # Route through router/commands/ grammar for verb detection.
    grammar_cmd = parse_from_message(text)
    if grammar_cmd is None or grammar_cmd.verb not in _PACK_VERBS:
        return False

    # Grammar identified a pack verb.  Build the structured command object
    # for dispatch, using the grammar Command's args directly.
    g = grammar_cmd
    if g.verb == "grant":
        if len(g.args) < 2:
            await say("Usage: grant <agent> <pack>")
            return True
        cmd: PackCommand = GrantCommand(agent=g.args[0].lower(), pack=g.args[1].lower())
    elif g.verb == "revoke":
        if len(g.args) < 2:
            await say("Usage: revoke <agent> <pack>")
            return True
        cmd = RevokeCommand(agent=g.args[0].lower(), pack=g.args[1].lower())
    elif g.verb == "list packs":
        cmd = ListPacksCommand()
    elif g.verb == "who has":
        if not g.args:
            await say("Usage: who has <pack>")
            return True
        cmd = WhoHasCommand(pack=g.args[0].lower())
    else:
        return False

    if isinstance(cmd, GrantCommand):
        await handle_grant(
            cmd,
            say,
            packs_dir=packs_dir,
            agents_dir=agents_dir,
            secret_store=secret_store,
            channel=channel,
            thread_ts=thread_ts,
        )
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


async def _run_install(pack: Pack) -> None:
    """Execute ``pack/install.sh`` on the host. Raises on non-zero exit.

    The script provisions host-side prerequisites — typically a CLI binary
    in the shared ``agent-tools`` Docker volume. We invoke it via
    ``bash`` (rather than relying on the file's exec bit) so this works
    on filesystems that strip permissions, e.g. some bind mounts.
    """
    proc = await asyncio.create_subprocess_exec(
        "bash",
        str(pack.install_path),
        cwd=str(pack.install_path.parent.parent.parent),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        tail = (stdout.decode(errors="replace") or "")[-500:]
        raise RuntimeError(f"install.sh exited {proc.returncode}: {tail.strip()}")
    logger.info("install.sh for pack %s completed successfully", pack.name)


def _read_packs_list(agent_yaml: Path) -> list[str]:
    with open(agent_yaml) as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        return []
    value = raw.get("packs") or []
    return list(value) if isinstance(value, list) else []


# Match an existing top-level ``packs:`` block including its body.
# Consumes indented lines (pack list items) and blank-only lines that are
# followed by another newline or EOF.  The lookahead ``(?=\n|\Z)`` on the
# blank-line alternative ensures the newline that *precedes* the next
# top-level key is never consumed, so that key cannot be fused into the
# last pack list item.
_PACKS_BLOCK_RE = re.compile(
    r"(?m)^packs\s*:(?:[^\n]*)(?:\n[ \t][^\n]*|\n[ \t]*(?=\n|\Z))*",
)


def _render_packs_block(packs: list[str]) -> str:
    if not packs:
        return "packs: []\n"
    return "packs:\n" + "\n".join(f"  - {p}" for p in sorted(packs)) + "\n"


def _replace_or_append_packs(text: str, new_block: str) -> str:
    if _PACKS_BLOCK_RE.search(text):
        return _PACKS_BLOCK_RE.sub(new_block, text, count=1)
    if not text.endswith("\n"):
        text += "\n"
    # new_block is already newline-terminated; one extra \n for readability.
    return text + "\n" + new_block


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
