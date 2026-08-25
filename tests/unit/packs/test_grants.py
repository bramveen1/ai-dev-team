"""Unit tests for router.packs.grants — the grant flow.

Tests use tmp_path-based agents/packs/secrets directories so they don't
depend on the real ``config/agents/*`` or ``packs/*`` layout.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest
import yaml

from router.packs.grants import (
    GrantCommand,
    InputPrompt,
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


def _make_fake_adapter(replies: list[str] | None = None, *, supports_forms: bool = False):
    """Minimal in-memory ChatAdapter (same shape as the #122 parity fixture).

    Queued ``replies`` are delivered FIFO: each lands in history the next time
    the collector polls ``read_thread`` after posting a prompt — mirroring
    "the user answers after seeing the prompt."
    """
    from router.chat.interface import ChatAdapter
    from router.chat.types import (
        AdapterCapabilities,
        ConversationRef,
        InboundMessage,
        InputResponse,
        OutboundMessage,
        PrincipalRef,
        StructuredResponse,
    )

    class FakeAdapter(ChatAdapter):
        def __init__(self):
            self.history: list = []
            self.sent: list[str] = []
            self._pending: list[str] = list(replies or [])
            self.collect_input_calls: list = []

        @property
        def capabilities(self):
            return AdapterCapabilities(supports_forms=supports_forms)

        async def send_message(self, outbound):
            self.sent.append(outbound.text)
            self.history.append(outbound)

        async def read_thread(self, conversation_ref):
            if self._pending and self.history and isinstance(self.history[-1], OutboundMessage):
                self.history.append(
                    InboundMessage(
                        conversation_ref=ConversationRef("fake:1"),
                        principal_ref=PrincipalRef("u1"),
                        text=self._pending.pop(0),
                    )
                )
            return list(self.history)

        async def set_status(self, conversation_ref, state):
            pass

        def resolve_principal(self, raw_user_id):
            return PrincipalRef(raw_user_id)

        def parse_mentions(self, text, conversation_ref):
            return []

        async def prompt_for_choice(self, conversation_ref, prompt):
            return StructuredResponse(choice=prompt.choices[0], index=0)

        async def collect_input(self, conversation_ref, request):
            # Form-capable stub: record the request and answer every field
            # with the next queued reply.
            self.collect_input_calls.append(request)
            values = {f.key: self._pending.pop(0) for f in request.fields}
            return InputResponse(values=values, status="completed")

    return FakeAdapter()


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
    async def test_already_granted_short_circuits_when_fully_provisioned(self, env) -> None:
        """Pack in manifest *and* secret on file → short-circuit, no auth re-run."""
        agents_dir, packs_dir, store = env
        _write_pack(packs_dir, "github", manifest="name: github\nneeds: [GITHUB_TOKEN]")
        _write_agent_manifest(agents_dir / "sam" / "agent.yaml", "name: Sam\npacks:\n  - github\n")
        store.set("github", {"GITHUB_TOKEN": "ghp_existing"})
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
    async def test_re_grant_recovers_missing_secret(self, env) -> None:
        """Pack in manifest but secret missing → re-run authenticate to recover.

        This guards the case where a PR seeded ``packs:`` ahead of the user
        running the grant flow, or where the secret was rotated/deleted.
        """
        agents_dir, packs_dir, store = env
        _write_pack(
            packs_dir,
            "zoho-mail",
            manifest="name: zoho-mail\nneeds: [ZOHO_API_KEY]",
            files={
                "authenticate.py": textwrap.dedent("""\
                    async def acquire(say):
                        return {"ZOHO_API_KEY": "recovered"}
                    """),
            },
        )
        _write_agent_manifest(agents_dir / "lisa" / "agent.yaml", "name: Lisa\npacks:\n  - zoho-mail\n")
        say = FakeSay()
        await handle_grant(
            GrantCommand(agent="lisa", pack="zoho-mail"),
            say,
            packs_dir=packs_dir,
            agents_dir=agents_dir,
            secret_store=store,
        )
        assert store.get("zoho-mail") == {"ZOHO_API_KEY": "recovered"}
        assert "Granted" in say.joined
        assert "Restored the missing token" in say.joined

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
    async def test_grant_authenticate_returns_empty_dict_blocks_grant(self, env) -> None:
        """acquire() returning {} with non-empty needs → :x: failure, no manifest write, no secret stored."""
        agents_dir, packs_dir, store = env
        _write_pack(
            packs_dir,
            "github",
            manifest="name: github\nneeds: [GITHUB_TOKEN]",
            files={
                "authenticate.py": textwrap.dedent("""\
                    async def acquire(say):
                        return {}
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
        assert ":x:" in say.joined
        assert "GITHUB_TOKEN" in say.joined
        assert "Granted" not in say.joined
        assert manifest.read_text() == original
        assert not store.has("github")

    @pytest.mark.asyncio
    async def test_grant_authenticate_returns_none_blocks_grant(self, env) -> None:
        """acquire() returning None with non-empty needs → :x: failure, no manifest write, no secret stored."""
        agents_dir, packs_dir, store = env
        _write_pack(
            packs_dir,
            "github",
            manifest="name: github\nneeds: [GITHUB_TOKEN]",
            files={
                "authenticate.py": textwrap.dedent("""\
                    async def acquire(say):
                        return None
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
        assert ":x:" in say.joined
        assert "GITHUB_TOKEN" in say.joined
        assert "Granted" not in say.joined
        assert manifest.read_text() == original
        assert not store.has("github")

    @pytest.mark.asyncio
    async def test_grant_authenticate_returns_partial_dict_blocks_grant(self, env) -> None:
        """acquire() returning a dict missing one of pack.needs → :x: failure naming missing keys."""
        agents_dir, packs_dir, store = env
        _write_pack(
            packs_dir,
            "zoho-mail",
            manifest="name: zoho-mail\nneeds: [ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET]",
            files={
                "authenticate.py": textwrap.dedent("""\
                    async def acquire(say):
                        return {"ZOHO_CLIENT_ID": "id-only"}
                    """),
            },
        )
        manifest = _write_agent_manifest(agents_dir / "lisa" / "agent.yaml", "name: Lisa\n")
        original = manifest.read_text()
        say = FakeSay()
        await handle_grant(
            GrantCommand(agent="lisa", pack="zoho-mail"),
            say,
            packs_dir=packs_dir,
            agents_dir=agents_dir,
            secret_store=store,
        )
        assert ":x:" in say.joined
        assert "ZOHO_CLIENT_SECRET" in say.joined
        assert "Granted" not in say.joined
        assert manifest.read_text() == original
        assert not store.has("zoho-mail")

    @pytest.mark.asyncio
    async def test_grant_authenticate_returns_full_dict_succeeds(self, env) -> None:
        """acquire() satisfying all pack.needs → success, secret stored, manifest updated."""
        agents_dir, packs_dir, store = env
        _write_pack(
            packs_dir,
            "zoho-mail",
            manifest="name: zoho-mail\nneeds: [ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET]",
            files={
                "authenticate.py": textwrap.dedent("""\
                    async def acquire(say):
                        return {"ZOHO_CLIENT_ID": "id", "ZOHO_CLIENT_SECRET": "secret"}
                    """),
            },
        )
        manifest = _write_agent_manifest(agents_dir / "lisa" / "agent.yaml", "name: Lisa\n")
        say = FakeSay()
        await handle_grant(
            GrantCommand(agent="lisa", pack="zoho-mail"),
            say,
            packs_dir=packs_dir,
            agents_dir=agents_dir,
            secret_store=store,
        )
        assert "Granted" in say.joined
        assert store.get("zoho-mail") == {"ZOHO_CLIENT_ID": "id", "ZOHO_CLIENT_SECRET": "secret"}
        assert yaml.safe_load(manifest.read_text())["packs"] == ["zoho-mail"]

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

    @pytest.mark.asyncio
    async def test_grant_runs_install_sh_when_present(self, env, tmp_path) -> None:
        """A pack that ships install.sh has it executed during grant —
        this is what closes the "pack granted but CLI never installed" gap."""
        agents_dir, packs_dir, store = env
        marker = tmp_path / "installed.marker"
        _write_pack(
            packs_dir,
            "withcli",
            manifest="name: withcli",
            files={
                "install.sh": f"#!/usr/bin/env bash\nset -eu\ntouch '{marker}'\necho ok\n",
            },
        )
        # File needs to be readable; bash invocation in _run_install
        # doesn't require the +x bit.
        _write_agent_manifest(agents_dir / "sam" / "agent.yaml", "name: Sam\n")
        say = FakeSay()
        await handle_grant(
            GrantCommand(agent="sam", pack="withcli"),
            say,
            packs_dir=packs_dir,
            agents_dir=agents_dir,
            secret_store=store,
        )
        assert marker.exists(), "install.sh should have been executed"
        assert "install.sh" in say.joined
        assert "Granted" in say.joined

    @pytest.mark.asyncio
    async def test_grant_install_sh_failure_blocks_grant(self, env) -> None:
        """If install.sh exits non-zero, surface the error and don't claim
        success. The manifest still gets the pack appended (the auth flow
        already succeeded), but the user sees a clear failure message and
        can re-run grant after fixing the host environment."""
        agents_dir, packs_dir, store = env
        _write_pack(
            packs_dir,
            "brokencli",
            manifest="name: brokencli",
            files={
                "install.sh": "#!/usr/bin/env bash\nset -eu\necho 'something went wrong' >&2\nexit 7\n",
            },
        )
        _write_agent_manifest(agents_dir / "sam" / "agent.yaml", "name: Sam\n")
        say = FakeSay()
        await handle_grant(
            GrantCommand(agent="sam", pack="brokencli"),
            say,
            packs_dir=packs_dir,
            agents_dir=agents_dir,
            secret_store=store,
        )
        joined = say.joined
        assert "install.sh" in joined
        assert "failed" in joined.lower()
        assert "Granted" not in joined


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
            "aidt list packs",
            say,
            packs_dir=packs_dir,
            agents_dir=tmp_path / "agents",
            secret_store=SecretStore(path=tmp_path / "s.json"),
        )
        assert handled is True
        assert "github" in say.joined

    @pytest.mark.asyncio
    async def test_bare_pack_verb_text_without_aidt_falls_through(self, tmp_path: Path) -> None:
        """#735: ordinary messages shaped like a pack verb must not be
        swallowed unless they carry the ``aidt`` marker."""
        say = FakeSay()
        handled = await maybe_handle_pack_command(
            "grant me access please",
            say,
            packs_dir=tmp_path / "packs",
            agents_dir=tmp_path / "agents",
            secret_store=SecretStore(path=tmp_path / "s.json"),
        )
        assert handled is False
        assert say.messages == []


# ── InputPrompt (InputRequest-backed prompt, #747) ───────────────────


class TestInputPrompt:
    @pytest.mark.asyncio
    async def test_call_proxies_to_say(self) -> None:
        say = FakeSay()
        prompt = InputPrompt(say)
        await prompt("hello")
        assert say.messages == ["hello"]

    @pytest.mark.asyncio
    async def test_prompt_without_adapter_raises(self) -> None:
        prompt = InputPrompt(FakeSay())
        with pytest.raises(RuntimeError, match="requires an adapter"):
            await prompt.prompt("paste your token")

    @pytest.mark.asyncio
    async def test_prompt_collects_reply_via_scripted_fallback(self) -> None:
        from router.chat.types import ConversationRef

        adapter = _make_fake_adapter(replies=["ghp_xyz"])
        prompt = InputPrompt(FakeSay(), adapter=adapter, conversation_ref=ConversationRef("fake:1"))

        result = await prompt.prompt("paste your token", timeout=5)

        assert result == "ghp_xyz"
        assert any("paste your token" in text for text in adapter.sent)
        # SECRET field → the scripted fallback posts the visibility warning.
        assert any("visible in the channel history" in text for text in adapter.sent)

    @pytest.mark.asyncio
    async def test_prompt_uses_native_form_when_supported(self) -> None:
        from router.chat.types import ConversationRef, InputFieldType

        adapter = _make_fake_adapter(replies=["ghp_native"], supports_forms=True)
        prompt = InputPrompt(FakeSay(), adapter=adapter, conversation_ref=ConversationRef("fake:1"))

        result = await prompt.prompt("paste your token")

        assert result == "ghp_native"
        assert len(adapter.collect_input_calls) == 1
        request = adapter.collect_input_calls[0]
        assert [f.type for f in request.fields] == [InputFieldType.SECRET]

    @pytest.mark.asyncio
    async def test_prompt_times_out(self) -> None:
        from router.chat.types import ConversationRef

        adapter = _make_fake_adapter(replies=[])
        prompt = InputPrompt(FakeSay(), adapter=adapter, conversation_ref=ConversationRef("fake:1"))
        with pytest.raises(asyncio.TimeoutError):
            await prompt.prompt("paste it", timeout=0.05)

    @pytest.mark.asyncio
    async def test_prompt_cancel_raises(self) -> None:
        from router.chat.types import ConversationRef

        adapter = _make_fake_adapter(replies=["cancel"])
        prompt = InputPrompt(FakeSay(), adapter=adapter, conversation_ref=ConversationRef("fake:1"))
        with pytest.raises(RuntimeError, match="cancelled"):
            await prompt.prompt("paste it", timeout=5)


class TestGrantScriptedTransportParity:
    """A no-modal transport fulfils the pack-grant prompt via scripted Q&A (#747)."""

    @pytest.mark.asyncio
    async def test_grant_prompt_flow_over_scripted_adapter(self, tmp_path: Path) -> None:
        from router.chat.types import ConversationRef

        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        agents_dir.mkdir()
        packs_dir.mkdir()
        store = SecretStore(path=tmp_path / "secrets.json")
        _write_pack(
            packs_dir,
            "github",
            manifest="name: github\nneeds: [GITHUB_TOKEN]",
            files={
                "authenticate.py": textwrap.dedent("""\
                    async def acquire(say):
                        token = await say.prompt("Paste the token")
                        return {"GITHUB_TOKEN": token}
                    """),
            },
        )
        manifest = _write_agent_manifest(agents_dir / "sam" / "agent.yaml", "name: Sam\n")

        adapter = _make_fake_adapter(replies=["ghp_scripted"])
        say = FakeSay()
        await handle_grant(
            GrantCommand(agent="sam", pack="github"),
            say,
            packs_dir=packs_dir,
            agents_dir=agents_dir,
            secret_store=store,
            adapter=adapter,
            conversation_ref=ConversationRef("fake:1"),
        )

        assert store.get("github") == {"GITHUB_TOKEN": "ghp_scripted"}
        assert yaml.safe_load(manifest.read_text())["packs"] == ["github"]
        assert "Granted" in say.joined
        assert any("Paste the token" in text for text in adapter.sent)


# ── round-trip key-preservation (regression for #400) ───────────────


class TestRoundTripKeyPreservation:
    """grant/revoke must not drop or fuse the key that follows packs:.

    Covers both layouts (blank line / no blank line between packs: and the
    next top-level key) for both grant and revoke, per issue #400.
    """

    @pytest.fixture()
    def env(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        agents_dir.mkdir()
        packs_dir.mkdir()
        _write_pack(packs_dir, "github")
        return agents_dir, packs_dir, SecretStore(path=tmp_path / "secrets.json")

    @pytest.mark.asyncio
    async def test_grant_preserves_keys_no_blank_line(self, env) -> None:
        agents_dir, packs_dir, store = env
        manifest = _write_agent_manifest(
            agents_dir / "sam" / "agent.yaml",
            "name: sam\npacks:\n  - slack\nmodel: opus\n",
        )
        await handle_grant(
            GrantCommand(agent="sam", pack="github"),
            FakeSay(),
            packs_dir=packs_dir,
            agents_dir=agents_dir,
            secret_store=store,
        )
        loaded = yaml.safe_load(manifest.read_text())
        assert set(loaded.keys()) == {"name", "packs", "model"}
        assert "github" in loaded["packs"]
        assert "slack" in loaded["packs"]

    @pytest.mark.asyncio
    async def test_grant_preserves_keys_blank_line(self, env) -> None:
        agents_dir, packs_dir, store = env
        manifest = _write_agent_manifest(
            agents_dir / "sam" / "agent.yaml",
            "name: sam\npacks:\n  - slack\n\nmodel: opus\n",
        )
        await handle_grant(
            GrantCommand(agent="sam", pack="github"),
            FakeSay(),
            packs_dir=packs_dir,
            agents_dir=agents_dir,
            secret_store=store,
        )
        loaded = yaml.safe_load(manifest.read_text())
        assert set(loaded.keys()) == {"name", "packs", "model"}
        assert "github" in loaded["packs"]
        assert "slack" in loaded["packs"]

    @pytest.mark.asyncio
    async def test_revoke_preserves_keys_no_blank_line(self, env) -> None:
        agents_dir, packs_dir, store = env
        manifest = _write_agent_manifest(
            agents_dir / "sam" / "agent.yaml",
            "name: sam\npacks:\n  - github\n  - slack\nmodel: opus\n",
        )
        await handle_revoke(
            RevokeCommand(agent="sam", pack="github"),
            FakeSay(),
            agents_dir=agents_dir,
            secret_store=store,
        )
        loaded = yaml.safe_load(manifest.read_text())
        assert set(loaded.keys()) == {"name", "packs", "model"}
        assert loaded["packs"] == ["slack"]

    @pytest.mark.asyncio
    async def test_revoke_preserves_keys_blank_line(self, env) -> None:
        agents_dir, packs_dir, store = env
        manifest = _write_agent_manifest(
            agents_dir / "sam" / "agent.yaml",
            "name: sam\npacks:\n  - github\n  - slack\n\nmodel: opus\n",
        )
        await handle_revoke(
            RevokeCommand(agent="sam", pack="github"),
            FakeSay(),
            agents_dir=agents_dir,
            secret_store=store,
        )
        loaded = yaml.safe_load(manifest.read_text())
        assert set(loaded.keys()) == {"name", "packs", "model"}
        assert loaded["packs"] == ["slack"]


class TestAtomicWriteValidatedKeyGuard:
    """``_atomic_write_validated`` must refuse an edit that drops a sibling
    key of ``packs:``, even when the result is syntactically valid YAML —
    the failure mode that let #382 corrupt manifests silently."""

    def test_raises_when_sibling_key_is_lost(self, tmp_path: Path) -> None:
        from router.packs.grants import _atomic_write_validated

        path = tmp_path / "agent.yaml"
        path.write_text("name: sam\npacks:\n  - github\nmodel: opus\n")
        original = path.read_text()
        # Valid YAML, but semantically corrupt: `model` was fused away.
        corrupted = "name: sam\npacks:\n  - github\n  - slackmodel: opus\n"

        with pytest.raises(RuntimeError, match="model"):
            _atomic_write_validated(path, original, corrupted)

        # Refused edits must not touch disk.
        assert path.read_text() == original

    def test_allows_edit_that_preserves_all_sibling_keys(self, tmp_path: Path) -> None:
        from router.packs.grants import _atomic_write_validated

        path = tmp_path / "agent.yaml"
        path.write_text("name: sam\npacks:\n  - github\nmodel: opus\n")
        original = path.read_text()
        updated = "name: sam\npacks:\n  - github\n  - slack\nmodel: opus\n"

        _atomic_write_validated(path, original, updated)

        assert path.read_text() == updated


class TestPendingInputRegistry:
    """The pending-reply consumption contract now lives in ``router.chat.pending_input``."""

    def test_returns_false_when_no_pending(self) -> None:
        from router.chat import pending_input

        assert pending_input.resolve_reply("slack:C1:t.unrelated", "anything") is False

    @pytest.mark.asyncio
    async def test_returns_false_when_already_resolved(self) -> None:
        from router.chat import pending_input

        async def deliver_twice():
            await asyncio.sleep(0)
            assert pending_input.resolve_reply("slack:C1:t.idempotent", "first") is True
            # Future already done — second call no-ops.
            assert pending_input.resolve_reply("slack:C1:t.idempotent", "second") is False

        result, _ = await asyncio.gather(
            pending_input.wait_for_reply("slack:C1:t.idempotent", 5),
            deliver_twice(),
        )
        assert result == "first"
