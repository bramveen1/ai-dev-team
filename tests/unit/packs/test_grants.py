"""Unit tests for router.packs.grants — the Slack grant flow.

Tests use tmp_path-based agents/packs/secrets directories so they don't
depend on the real ``config/agents/*`` or ``packs/*`` layout.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from router.packs.grants import (
    GrantCommand,
    ListPacksCommand,
    RevokeCommand,
    WhoHasCommand,
    handle_grant,
    handle_list_packs,
    handle_revoke,
    handle_who_has,
    maybe_handle_pack_command,
    parse_command,
)
from router.packs.secret_store import SecretStore

pytestmark = pytest.mark.unit


def _write_agent_manifest(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip())
    return path


def _write_pack(
    packs_dir: Path,
    name: str,
    *,
    manifest: str | None = None,
    files: dict[str, str] | None = None,
) -> Path:
    pack_dir = packs_dir / name
    pack_dir.mkdir(parents=True)
    (pack_dir / "pack.yaml").write_text(textwrap.dedent(manifest or f"name: {name}").strip() + "\n")
    for filename, content in (files or {}).items():
        (pack_dir / filename).write_text(content)
    return pack_dir


class FakeSay:
    """Async ``say`` collector for assertions."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def __call__(self, text: str) -> None:
        self.messages.append(text)

    @property
    def joined(self) -> str:
        return "\n".join(self.messages)


# ── parse_command ────────────────────────────────────────────────────


class TestParseCommand:
    def test_grant_lowercases(self) -> None:
        cmd = parse_command("grant Sam Github")
        assert cmd == GrantCommand(agent="sam", pack="github")

    def test_grant_with_mention_prefix(self) -> None:
        cmd = parse_command("<@U123XYZ> grant sam github")
        assert cmd == GrantCommand(agent="sam", pack="github")

    def test_revoke(self) -> None:
        assert parse_command("revoke sam github") == RevokeCommand(agent="sam", pack="github")

    def test_list_packs(self) -> None:
        assert parse_command("list packs") == ListPacksCommand()

    def test_who_has(self) -> None:
        assert parse_command("who has github") == WhoHasCommand(pack="github")

    def test_pack_name_with_hyphen(self) -> None:
        assert parse_command("grant lisa zoho-mail") == GrantCommand(agent="lisa", pack="zoho-mail")

    def test_extra_whitespace(self) -> None:
        assert parse_command("  grant   sam   github  ") == GrantCommand(agent="sam", pack="github")

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "hello there",
            "grant sam",  # missing pack
            "grant",
            "list pack",  # singular
            "who has",
            "who has it pls",
            "Tell me about the grant flow",
        ],
    )
    def test_non_commands_return_none(self, text: str) -> None:
        assert parse_command(text) is None


# ── handle_grant ─────────────────────────────────────────────────────


class TestHandleGrant:
    @pytest.fixture()
    def env(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        secrets_path = tmp_path / "secrets.json"
        agents_dir.mkdir()
        packs_dir.mkdir()
        return agents_dir, packs_dir, SecretStore(path=secrets_path)

    @pytest.mark.asyncio
    async def test_unknown_pack(self, env) -> None:
        agents_dir, packs_dir, store = env
        say = FakeSay()
        await handle_grant(
            GrantCommand(agent="sam", pack="ghost"),
            say,
            packs_dir=packs_dir,
            agents_dir=agents_dir,
            secret_store=store,
        )
        assert "not found" in say.joined.lower()

    @pytest.mark.asyncio
    async def test_unknown_agent(self, env) -> None:
        agents_dir, packs_dir, store = env
        _write_pack(packs_dir, "github")
        say = FakeSay()
        await handle_grant(
            GrantCommand(agent="ghost", pack="github"),
            say,
            packs_dir=packs_dir,
            agents_dir=agents_dir,
            secret_store=store,
        )
        assert "Agent `ghost` not found" in say.joined

    @pytest.mark.asyncio
    async def test_already_granted(self, env) -> None:
        agents_dir, packs_dir, store = env
        _write_pack(packs_dir, "github")
        _write_agent_manifest(agents_dir / "sam" / "agent.yaml", "name: Sam\npacks:\n  - github\n")
        say = FakeSay()
        await handle_grant(
            GrantCommand(agent="sam", pack="github"),
            say,
            packs_dir=packs_dir,
            agents_dir=agents_dir,
            secret_store=store,
        )
        assert "already has" in say.joined

    @pytest.mark.asyncio
    async def test_grant_appends_packs_block_when_missing(self, env) -> None:
        agents_dir, packs_dir, store = env
        _write_pack(packs_dir, "github")
        manifest = _write_agent_manifest(
            agents_dir / "sam" / "agent.yaml",
            """\
            # Sam — agent manifest.
            name: Sam
            container: sam
            thinking_status: "is digging in…"
            capabilities: {}
            """,
        )
        say = FakeSay()
        await handle_grant(
            GrantCommand(agent="sam", pack="github"),
            say,
            packs_dir=packs_dir,
            agents_dir=agents_dir,
            secret_store=store,
        )
        assert "Granted" in say.joined
        # Round-trip parse
        loaded = yaml.safe_load(manifest.read_text())
        assert loaded["packs"] == ["github"]
        # Existing comments / fields preserved
        assert "# Sam — agent manifest." in manifest.read_text()
        assert "capabilities: {}" in manifest.read_text()

    @pytest.mark.asyncio
    async def test_grant_extends_existing_packs_block(self, env) -> None:
        agents_dir, packs_dir, store = env
        _write_pack(packs_dir, "posthog")
        manifest = _write_agent_manifest(
            agents_dir / "sam" / "agent.yaml",
            """\
            name: Sam
            packs:
              - github
            """,
        )
        say = FakeSay()
        await handle_grant(
            GrantCommand(agent="sam", pack="posthog"),
            say,
            packs_dir=packs_dir,
            agents_dir=agents_dir,
            secret_store=store,
        )
        loaded = yaml.safe_load(manifest.read_text())
        assert loaded["packs"] == ["github", "posthog"]

    @pytest.mark.asyncio
    async def test_grant_runs_authenticate_and_stores_secret(self, env) -> None:
        agents_dir, packs_dir, store = env
        _write_pack(
            packs_dir,
            "github",
            manifest="name: github\nneeds: [GITHUB_TOKEN]",
            files={
                "authenticate.py": textwrap.dedent("""\
                    async def acquire(say):
                        await say("got it")
                        return {"GITHUB_TOKEN": "ghp_test"}
                    """),
            },
        )
        _write_agent_manifest(agents_dir / "sam" / "agent.yaml", "name: Sam\n")
        say = FakeSay()
        await handle_grant(
            GrantCommand(agent="sam", pack="github"),
            say,
            packs_dir=packs_dir,
            agents_dir=agents_dir,
            secret_store=store,
        )
        assert store.get("github") == {"GITHUB_TOKEN": "ghp_test"}
        assert "got it" in say.joined
        assert "Granted" in say.joined

    @pytest.mark.asyncio
    async def test_grant_authenticate_failure_does_not_modify_manifest(self, env) -> None:
        agents_dir, packs_dir, store = env
        _write_pack(
            packs_dir,
            "github",
            manifest="name: github\nneeds: [GITHUB_TOKEN]",
            files={
                "authenticate.py": textwrap.dedent("""\
                    async def acquire(say):
                        raise RuntimeError("nope")
                    """),
            },
        )
        manifest = _write_agent_manifest(agents_dir / "sam" / "agent.yaml", "name: Sam\n")
        original = manifest.read_text()
        say = FakeSay()
        await handle_grant(
            GrantCommand(agent="sam", pack="github"),
            say,
            packs_dir=packs_dir,
            agents_dir=agents_dir,
            secret_store=store,
        )
        assert "failed" in say.joined.lower()
        assert manifest.read_text() == original
        assert not store.has("github")

    @pytest.mark.asyncio
    async def test_grant_pack_with_needs_but_no_authenticate_warns(self, env) -> None:
        agents_dir, packs_dir, store = env
        _write_pack(packs_dir, "manual", manifest="name: manual\nneeds: [MANUAL_TOKEN]")
        manifest = _write_agent_manifest(agents_dir / "sam" / "agent.yaml", "name: Sam\n")
        original = manifest.read_text()
        say = FakeSay()
        await handle_grant(
            GrantCommand(agent="sam", pack="manual"),
            say,
            packs_dir=packs_dir,
            agents_dir=agents_dir,
            secret_store=store,
        )
        assert "manually" in say.joined.lower()
        # Manifest unchanged because we couldn't acquire secrets.
        assert manifest.read_text() == original

    @pytest.mark.asyncio
    async def test_grant_pack_with_no_needs_succeeds_without_authenticate(self, env) -> None:
        agents_dir, packs_dir, store = env
        _write_pack(packs_dir, "freebie")
        manifest = _write_agent_manifest(agents_dir / "sam" / "agent.yaml", "name: Sam\n")
        say = FakeSay()
        await handle_grant(
            GrantCommand(agent="sam", pack="freebie"),
            say,
            packs_dir=packs_dir,
            agents_dir=agents_dir,
            secret_store=store,
        )
        assert "Granted" in say.joined
        assert yaml.safe_load(manifest.read_text())["packs"] == ["freebie"]


# ── handle_revoke ────────────────────────────────────────────────────


class TestHandleRevoke:
    @pytest.fixture()
    def env(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        store = SecretStore(path=tmp_path / "secrets.json")
        return agents_dir, store

    @pytest.mark.asyncio
    async def test_unknown_agent(self, env) -> None:
        agents_dir, store = env
        say = FakeSay()
        await handle_revoke(
            RevokeCommand(agent="ghost", pack="github"),
            say,
            agents_dir=agents_dir,
            secret_store=store,
        )
        assert "not found" in say.joined.lower()

    @pytest.mark.asyncio
    async def test_pack_not_present(self, env) -> None:
        agents_dir, store = env
        _write_agent_manifest(agents_dir / "sam" / "agent.yaml", "name: Sam\n")
        say = FakeSay()
        await handle_revoke(
            RevokeCommand(agent="sam", pack="github"),
            say,
            agents_dir=agents_dir,
            secret_store=store,
        )
        assert "doesn't have" in say.joined

    @pytest.mark.asyncio
    async def test_removes_pack_keeps_others(self, env) -> None:
        agents_dir, store = env
        manifest = _write_agent_manifest(
            agents_dir / "sam" / "agent.yaml",
            """\
            name: Sam
            packs:
              - github
              - posthog
            """,
        )
        say = FakeSay()
        await handle_revoke(
            RevokeCommand(agent="sam", pack="github"),
            say,
            agents_dir=agents_dir,
            secret_store=store,
        )
        assert yaml.safe_load(manifest.read_text())["packs"] == ["posthog"]

    @pytest.mark.asyncio
    async def test_removes_only_pack_yields_empty_list(self, env) -> None:
        agents_dir, store = env
        manifest = _write_agent_manifest(
            agents_dir / "sam" / "agent.yaml",
            """\
            name: Sam
            packs:
              - github
            """,
        )
        say = FakeSay()
        await handle_revoke(
            RevokeCommand(agent="sam", pack="github"),
            say,
            agents_dir=agents_dir,
            secret_store=store,
        )
        loaded = yaml.safe_load(manifest.read_text())
        assert loaded["packs"] == []

    @pytest.mark.asyncio
    async def test_drop_secret_when_requested(self, env) -> None:
        agents_dir, store = env
        store.set("github", {"GITHUB_TOKEN": "x"})
        _write_agent_manifest(
            agents_dir / "sam" / "agent.yaml",
            "name: Sam\npacks:\n  - github\n",
        )
        say = FakeSay()
        await handle_revoke(
            RevokeCommand(agent="sam", pack="github"),
            say,
            agents_dir=agents_dir,
            secret_store=store,
            drop_secret=True,
        )
        assert not store.has("github")
        assert "Secret block deleted" in say.joined


# ── handle_list_packs ────────────────────────────────────────────────


class TestHandleListPacks:
    @pytest.mark.asyncio
    async def test_empty(self, tmp_path: Path) -> None:
        say = FakeSay()
        await handle_list_packs(say, packs_dir=tmp_path / "packs")
        assert "No packs available" in say.joined

    @pytest.mark.asyncio
    async def test_lists_all(self, tmp_path: Path) -> None:
        packs_dir = tmp_path / "packs"
        packs_dir.mkdir()
        _write_pack(packs_dir, "github", manifest="name: github\ndescription: GitHub access")
        _write_pack(packs_dir, "posthog", manifest="name: posthog\ndescription: PostHog analytics")
        say = FakeSay()
        await handle_list_packs(say, packs_dir=packs_dir)
        assert "github" in say.joined
        assert "GitHub access" in say.joined
        assert "posthog" in say.joined


# ── handle_who_has ───────────────────────────────────────────────────


class TestHandleWhoHas:
    @pytest.mark.asyncio
    async def test_no_holders(self, tmp_path: Path) -> None:
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        say = FakeSay()
        await handle_who_has(WhoHasCommand(pack="github"), say, agents_dir=agents_dir)
        assert "No agent has" in say.joined

    @pytest.mark.asyncio
    async def test_finds_holders(self, tmp_path: Path) -> None:
        agents_dir = tmp_path / "agents"
        _write_agent_manifest(agents_dir / "sam" / "agent.yaml", "name: Sam\npacks: [github]\n")
        _write_agent_manifest(agents_dir / "max" / "agent.yaml", "name: Max\npacks: [github, jira]\n")
        _write_agent_manifest(agents_dir / "lisa" / "agent.yaml", "name: Lisa\npacks: [zoho-mail]\n")
        say = FakeSay()
        await handle_who_has(WhoHasCommand(pack="github"), say, agents_dir=agents_dir)
        assert "sam" in say.joined and "max" in say.joined
        assert "lisa" not in say.joined


# ── maybe_handle_pack_command ────────────────────────────────────────


class TestMaybeHandlePackCommand:
    @pytest.mark.asyncio
    async def test_returns_false_for_non_command(self, tmp_path: Path) -> None:
        say = FakeSay()
        handled = await maybe_handle_pack_command(
            "Hello there!",
            say,
            packs_dir=tmp_path / "packs",
            agents_dir=tmp_path / "agents",
            secret_store=SecretStore(path=tmp_path / "s.json"),
        )
        assert handled is False
        assert say.messages == []

    @pytest.mark.asyncio
    async def test_returns_true_for_list_packs(self, tmp_path: Path) -> None:
        packs_dir = tmp_path / "packs"
        packs_dir.mkdir()
        _write_pack(packs_dir, "github")
        say = FakeSay()
        handled = await maybe_handle_pack_command(
            "list packs",
            say,
            packs_dir=packs_dir,
            agents_dir=tmp_path / "agents",
            secret_store=SecretStore(path=tmp_path / "s.json"),
        )
        assert handled is True
        assert "github" in say.joined
