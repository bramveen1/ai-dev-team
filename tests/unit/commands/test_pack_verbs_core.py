"""Tests for pack-provisioning verbs (grant/revoke/list packs/who has) routed
through dispatch_command on the transport-neutral Discord path (issue #690).

Acceptance criteria from the issue:
- All four verbs return a real CommandResult through dispatch_command.
- No slack_sdk / say below the transport shim.
- Human-only gate still fires for all four on Discord.
- Interactive grant … authenticate … sub-flow returns explicit "not supported"
  message rather than a silent dead-end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from router.commands.core import dispatch_command
from router.commands.grammar import parse
from router.commands.types import CommandResult, Principal

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _human(ref: str = "discord:human-1") -> Principal:
    return Principal(ref=ref, kind="human")


def _bot(ref: str = "discord:bot-1") -> Principal:
    return Principal(ref=ref, kind="bot")


def _make_agent(agents_dir: Path, name: str, packs: list[str] | None = None) -> Path:
    agent_dir = agents_dir / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    pack_lines = "\n".join(f"  - {p}" for p in (packs or []))
    body = f"name: {name}\n"
    if packs:
        body += f"packs:\n{pack_lines}\n"
    else:
        body += "packs: []\n"
    (agent_dir / "agent.yaml").write_text(body)
    return agent_dir / "agent.yaml"


def _make_pack(packs_dir: Path, name: str, *, description: str = "test pack", needs: bool = False) -> Path:
    pack_dir = packs_dir / name
    pack_dir.mkdir(parents=True, exist_ok=True)
    manifest = f"name: {name}\ndescription: {description}\n"
    if needs:
        manifest += "needs:\n  - API_TOKEN\n"
    (pack_dir / "pack.yaml").write_text(manifest)
    return pack_dir


# ---------------------------------------------------------------------------
# Human-only gate for pack verbs
# ---------------------------------------------------------------------------


class TestPackVerbsHumanOnlyGate:
    @pytest.mark.asyncio
    async def test_bot_grant_rejected(self, tmp_path):
        cmd = parse("grant sam github", transport="discord")
        result = await dispatch_command(cmd, _bot(), agents_dir=tmp_path / "agents", packs_dir=tmp_path / "packs")
        assert not result.ok
        assert "restricted" in result.text or "agents cannot run commands" in result.text

    @pytest.mark.asyncio
    async def test_bot_revoke_rejected(self, tmp_path):
        cmd = parse("revoke sam github", transport="discord")
        result = await dispatch_command(cmd, _bot(), agents_dir=tmp_path / "agents", packs_dir=tmp_path / "packs")
        assert not result.ok

    @pytest.mark.asyncio
    async def test_bot_list_packs_rejected(self, tmp_path):
        cmd = parse("list packs", transport="discord")
        result = await dispatch_command(cmd, _bot(), packs_dir=tmp_path / "packs")
        assert not result.ok

    @pytest.mark.asyncio
    async def test_bot_who_has_rejected(self, tmp_path):
        cmd = parse("who has github", transport="discord")
        result = await dispatch_command(cmd, _bot(), agents_dir=tmp_path / "agents")
        assert not result.ok


# ---------------------------------------------------------------------------
# list packs — read-only, no agents dir needed
# ---------------------------------------------------------------------------


class TestListPacksCommand:
    @pytest.mark.asyncio
    async def test_no_packs_returns_message(self, tmp_path):
        empty_packs = tmp_path / "packs"
        empty_packs.mkdir()
        cmd = parse("list packs", transport="discord")
        result = await dispatch_command(cmd, _human(), packs_dir=empty_packs)
        assert isinstance(result, CommandResult)
        assert result.ok
        assert "No packs available" in result.text

    @pytest.mark.asyncio
    async def test_lists_available_packs(self, tmp_path):
        packs_dir = tmp_path / "packs"
        _make_pack(packs_dir, "github", description="GitHub integration")
        _make_pack(packs_dir, "brevo", description="Email via Brevo")
        cmd = parse("list packs", transport="discord")
        result = await dispatch_command(cmd, _human(), packs_dir=packs_dir)
        assert result.ok
        assert "github" in result.text
        assert "brevo" in result.text
        assert "GitHub integration" in result.text

    @pytest.mark.asyncio
    async def test_returns_command_result(self, tmp_path):
        packs_dir = tmp_path / "packs"
        _make_pack(packs_dir, "github")
        cmd = parse("list packs", transport="discord")
        result = await dispatch_command(cmd, _human(), packs_dir=packs_dir)
        assert isinstance(result, CommandResult)

    @pytest.mark.asyncio
    async def test_no_slack_sdk_on_path(self, tmp_path):
        """list packs must not import slack_sdk anywhere below dispatch_command."""
        import sys

        before = set(sys.modules.keys())
        packs_dir = tmp_path / "packs"
        _make_pack(packs_dir, "test-pack")
        cmd = parse("list packs", transport="discord")
        await dispatch_command(cmd, _human(), packs_dir=packs_dir)
        new_modules = set(sys.modules.keys()) - before
        slack_modules = [m for m in new_modules if "slack_sdk" in m or "slack_bolt" in m]
        assert not slack_modules, f"Unexpected slack_sdk imports: {slack_modules}"


# ---------------------------------------------------------------------------
# who has — read-only
# ---------------------------------------------------------------------------


class TestWhoHasCommand:
    @pytest.mark.asyncio
    async def test_no_agent_has_pack(self, tmp_path):
        agents_dir = tmp_path / "agents"
        _make_agent(agents_dir, "sam", packs=[])
        cmd = parse("who has github", transport="discord")
        result = await dispatch_command(cmd, _human(), agents_dir=agents_dir)
        assert result.ok
        assert "No agent has" in result.text

    @pytest.mark.asyncio
    async def test_one_agent_has_pack(self, tmp_path):
        agents_dir = tmp_path / "agents"
        _make_agent(agents_dir, "sam", packs=["github"])
        _make_agent(agents_dir, "lisa", packs=[])
        cmd = parse("who has github", transport="discord")
        result = await dispatch_command(cmd, _human(), agents_dir=agents_dir)
        assert result.ok
        assert "sam" in result.text
        assert "lisa" not in result.text

    @pytest.mark.asyncio
    async def test_multiple_agents_have_pack(self, tmp_path):
        agents_dir = tmp_path / "agents"
        _make_agent(agents_dir, "sam", packs=["github"])
        _make_agent(agents_dir, "lisa", packs=["github"])
        cmd = parse("who has github", transport="discord")
        result = await dispatch_command(cmd, _human(), agents_dir=agents_dir)
        assert result.ok
        assert "sam" in result.text
        assert "lisa" in result.text

    @pytest.mark.asyncio
    async def test_returns_command_result(self, tmp_path):
        agents_dir = tmp_path / "agents"
        _make_agent(agents_dir, "sam", packs=["github"])
        cmd = parse("who has github", transport="discord")
        result = await dispatch_command(cmd, _human(), agents_dir=agents_dir)
        assert isinstance(result, CommandResult)

    @pytest.mark.asyncio
    async def test_empty_agents_dir(self, tmp_path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        cmd = parse("who has github", transport="discord")
        result = await dispatch_command(cmd, _human(), agents_dir=agents_dir)
        assert result.ok
        assert "No agent has" in result.text


# ---------------------------------------------------------------------------
# grant — mutating
# ---------------------------------------------------------------------------


class TestGrantCommand:
    @pytest.mark.asyncio
    async def test_grant_unknown_pack(self, tmp_path):
        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        packs_dir.mkdir()
        _make_agent(agents_dir, "sam")
        cmd = parse("grant sam nosuchpack", transport="discord")
        result = await dispatch_command(cmd, _human(), agents_dir=agents_dir, packs_dir=packs_dir)
        assert not result.ok
        assert "not found" in result.text

    @pytest.mark.asyncio
    async def test_grant_unknown_agent(self, tmp_path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        packs_dir = tmp_path / "packs"
        _make_pack(packs_dir, "github")
        cmd = parse("grant nosuchagent github", transport="discord")
        result = await dispatch_command(cmd, _human(), agents_dir=agents_dir, packs_dir=packs_dir)
        assert not result.ok
        assert "not found" in result.text

    @pytest.mark.asyncio
    async def test_grant_pack_without_authenticate_succeeds(self, tmp_path):
        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        _make_agent(agents_dir, "sam", packs=[])
        _make_pack(packs_dir, "ops-diag")
        cmd = parse("grant sam ops-diag", transport="discord")
        result = await dispatch_command(cmd, _human(), agents_dir=agents_dir, packs_dir=packs_dir)
        assert result.ok
        assert "Granted" in result.text
        assert "sam" in result.text
        assert "ops-diag" in result.text

    @pytest.mark.asyncio
    async def test_grant_pack_with_authenticate_blocked_on_discord(self, tmp_path):
        """Packs with authenticate.py are not supported on Discord — must return error."""
        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        _make_agent(agents_dir, "sam", packs=[])
        pack_dir = _make_pack(packs_dir, "github")
        (pack_dir / "authenticate.py").write_text("async def acquire(say): return {}")
        cmd = parse("grant sam github", transport="discord")
        result = await dispatch_command(cmd, _human(), agents_dir=agents_dir, packs_dir=packs_dir)
        assert not result.ok
        assert "#552" in result.text
        assert "not supported" in result.text.lower()

    @pytest.mark.asyncio
    async def test_grant_idempotent_already_has_pack(self, tmp_path):
        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        _make_agent(agents_dir, "sam", packs=["ops-diag"])
        _make_pack(packs_dir, "ops-diag")
        cmd = parse("grant sam ops-diag", transport="discord")
        result = await dispatch_command(cmd, _human(), agents_dir=agents_dir, packs_dir=packs_dir)
        assert result.ok
        assert "already has" in result.text

    @pytest.mark.asyncio
    async def test_grant_missing_args_returns_error(self, tmp_path):
        cmd = parse("grant sam", transport="discord")
        # "grant sam" → args=["sam"], only one arg
        result = await dispatch_command(cmd, _human(), agents_dir=tmp_path / "agents", packs_dir=tmp_path / "packs")
        assert not result.ok

    @pytest.mark.asyncio
    async def test_grant_manifest_updated(self, tmp_path):
        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        agent_yaml = _make_agent(agents_dir, "sam", packs=[])
        _make_pack(packs_dir, "ops-diag")
        cmd = parse("grant sam ops-diag", transport="discord")
        result = await dispatch_command(cmd, _human(), agents_dir=agents_dir, packs_dir=packs_dir)
        assert result.ok
        import yaml

        manifest = yaml.safe_load(agent_yaml.read_text())
        assert "ops-diag" in manifest.get("packs", [])

    @pytest.mark.asyncio
    async def test_grant_returns_command_result(self, tmp_path):
        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        _make_agent(agents_dir, "sam", packs=[])
        _make_pack(packs_dir, "ops-diag")
        cmd = parse("grant sam ops-diag", transport="discord")
        result = await dispatch_command(cmd, _human(), agents_dir=agents_dir, packs_dir=packs_dir)
        assert isinstance(result, CommandResult)


# ---------------------------------------------------------------------------
# revoke — mutating
# ---------------------------------------------------------------------------


class TestRevokeCommand:
    @pytest.mark.asyncio
    async def test_revoke_unknown_agent(self, tmp_path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        cmd = parse("revoke nosuchagent github", transport="discord")
        result = await dispatch_command(cmd, _human(), agents_dir=agents_dir)
        assert not result.ok
        assert "not found" in result.text

    @pytest.mark.asyncio
    async def test_revoke_agent_doesnt_have_pack(self, tmp_path):
        agents_dir = tmp_path / "agents"
        _make_agent(agents_dir, "sam", packs=[])
        cmd = parse("revoke sam github", transport="discord")
        result = await dispatch_command(cmd, _human(), agents_dir=agents_dir)
        assert result.ok
        assert "doesn't have" in result.text

    @pytest.mark.asyncio
    async def test_revoke_removes_pack_from_manifest(self, tmp_path):
        agents_dir = tmp_path / "agents"
        agent_yaml = _make_agent(agents_dir, "sam", packs=["github", "ops-diag"])
        cmd = parse("revoke sam github", transport="discord")
        result = await dispatch_command(cmd, _human(), agents_dir=agents_dir)
        assert result.ok
        assert "Revoked" in result.text
        import yaml

        manifest = yaml.safe_load(agent_yaml.read_text())
        assert "github" not in manifest.get("packs", [])
        assert "ops-diag" in manifest.get("packs", [])

    @pytest.mark.asyncio
    async def test_revoke_missing_args_returns_error(self, tmp_path):
        cmd = parse("revoke sam", transport="discord")
        # "revoke sam" → args=["sam"], only one arg
        result = await dispatch_command(cmd, _human(), agents_dir=tmp_path / "agents")
        assert not result.ok

    @pytest.mark.asyncio
    async def test_revoke_returns_command_result(self, tmp_path):
        agents_dir = tmp_path / "agents"
        _make_agent(agents_dir, "sam", packs=["github"])
        cmd = parse("revoke sam github", transport="discord")
        result = await dispatch_command(cmd, _human(), agents_dir=agents_dir)
        assert isinstance(result, CommandResult)

    @pytest.mark.asyncio
    async def test_revoke_confirmation_message(self, tmp_path):
        agents_dir = tmp_path / "agents"
        _make_agent(agents_dir, "sam", packs=["github"])
        cmd = parse("revoke sam github", transport="discord")
        result = await dispatch_command(cmd, _human(), agents_dir=agents_dir)
        assert result.ok
        assert "sam" in result.text
        assert "github" in result.text


# ---------------------------------------------------------------------------
# Full aidt path — shim → dispatch_command (smoke test)
# ---------------------------------------------------------------------------


class TestAidtPackVerbsEndToEnd:
    @pytest.mark.asyncio
    async def test_aidt_list_packs_discord(self, tmp_path):
        from router.commands.discord_shim import parse_from_discord_message

        packs_dir = tmp_path / "packs"
        _make_pack(packs_dir, "github", description="GitHub pack")
        text = "aidt list packs"
        cmd = parse_from_discord_message(text, conversation_ref="discord:g:c:0")
        assert cmd is not None
        assert cmd.verb == "list packs"
        result = await dispatch_command(cmd, _human(), packs_dir=packs_dir)
        assert result.ok
        assert "github" in result.text

    @pytest.mark.asyncio
    async def test_aidt_who_has_discord(self, tmp_path):
        from router.commands.discord_shim import parse_from_discord_message

        agents_dir = tmp_path / "agents"
        _make_agent(agents_dir, "sam", packs=["github"])
        text = "aidt who has github"
        cmd = parse_from_discord_message(text, conversation_ref="discord:g:c:0")
        assert cmd is not None
        assert cmd.verb == "who has"
        result = await dispatch_command(cmd, _human(), agents_dir=agents_dir)
        assert result.ok
        assert "sam" in result.text

    @pytest.mark.asyncio
    async def test_aidt_grant_discord(self, tmp_path):
        from router.commands.discord_shim import parse_from_discord_message

        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        _make_agent(agents_dir, "sam", packs=[])
        _make_pack(packs_dir, "ops-diag")
        text = "aidt grant sam ops-diag"
        cmd = parse_from_discord_message(text, conversation_ref="discord:g:c:0")
        assert cmd is not None
        assert cmd.verb == "grant"
        result = await dispatch_command(cmd, _human(), agents_dir=agents_dir, packs_dir=packs_dir)
        assert result.ok
        assert "ops-diag" in result.text

    @pytest.mark.asyncio
    async def test_aidt_revoke_discord(self, tmp_path):
        from router.commands.discord_shim import parse_from_discord_message

        agents_dir = tmp_path / "agents"
        _make_agent(agents_dir, "sam", packs=["ops-diag"])
        text = "aidt revoke sam ops-diag"
        cmd = parse_from_discord_message(text, conversation_ref="discord:g:c:0")
        assert cmd is not None
        assert cmd.verb == "revoke"
        result = await dispatch_command(cmd, _human(), agents_dir=agents_dir)
        assert result.ok
        assert "ops-diag" in result.text

    @pytest.mark.asyncio
    async def test_aidt_grant_bot_rejected(self, tmp_path):
        """Bot principal on aidt grant must be rejected by the human-only gate."""
        from router.commands.discord_shim import parse_from_discord_message

        text = "aidt grant sam github"
        cmd = parse_from_discord_message(text)
        assert cmd is not None
        result = await dispatch_command(cmd, _bot())
        assert not result.ok
        assert "restricted" in result.text or "agents cannot run commands" in result.text
