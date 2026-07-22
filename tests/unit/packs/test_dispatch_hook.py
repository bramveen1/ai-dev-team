"""Unit tests for router.packs.dispatch_hook.

These tests use a tmp_path-based agents dir + packs dir so they don't
depend on the real ``config/agents/*`` or ``packs/*`` layout.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from router import settings as settings_mod
from router.packs.dispatch_hook import (
    CONTAINER_PACKS_DIR,
    pack_cli_extras,
)
from router.packs.secret_store import SecretStore
from router.settings import RuntimeSettings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_worker_tokens_env(monkeypatch):
    """Clear ``WORKERS_BOT_TOKEN`` / ``WORKERS_DISCORD_TOKEN`` from the env for
    every test in this module.

    ``pack_cli_extras`` now resolves both tokens env-first (``.env`` wins over
    the secret store). The dir-level ``conftest`` autouse fixture sets
    ``WORKERS_BOT_TOKEN`` so the dispatch handler's fail-fast doesn't trip, but
    that value would otherwise leak into every store-path assertion here.
    Clearing it restores the clean baseline; the precedence tests re-set the
    env vars explicitly to exercise the .env-wins path."""
    monkeypatch.delenv("WORKERS_BOT_TOKEN", raising=False)
    monkeypatch.delenv("WORKERS_DISCORD_TOKEN", raising=False)


def _write_agent_manifest(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).strip() + "\n")
    return path


def _write_pack(packs_dir: Path, name: str, *, manifest: str = None, files: dict[str, str] | None = None) -> Path:
    pack_dir = packs_dir / name
    pack_dir.mkdir(parents=True)
    (pack_dir / "pack.yaml").write_text(textwrap.dedent(manifest or f"name: {name}").strip() + "\n")
    for filename, content in (files or {}).items():
        (pack_dir / filename).write_text(content)
    return pack_dir


class TestPackCliExtrasDormant:
    """When agent.yaml has no ``packs:`` key, extras are empty."""

    def test_no_packs_key_returns_empty(self, tmp_path: Path) -> None:
        manifest = tmp_path / "lisa" / "agent.yaml"
        _write_agent_manifest(manifest, "name: Lisa\ncontainer: lisa")

        extras = pack_cli_extras(
            "lisa",
            manifest_path=manifest,
            packs_dir=tmp_path / "packs",
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
        )
        assert extras.prompt_files == []
        assert extras.mcp_config_path is None
        assert extras.env == {}

    def test_missing_manifest_returns_empty(self, tmp_path: Path) -> None:
        extras = pack_cli_extras(
            "ghost",
            manifest_path=tmp_path / "ghost.yaml",
            packs_dir=tmp_path / "packs",
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
        )
        assert extras.prompt_files == []
        assert extras.mcp_config_path is None
        assert extras.env == {}

    def test_empty_packs_list_returns_empty(self, tmp_path: Path) -> None:
        manifest = tmp_path / "sam" / "agent.yaml"
        _write_agent_manifest(manifest, "name: Sam\ncontainer: sam\npacks: []")

        extras = pack_cli_extras(
            "sam",
            manifest_path=manifest,
            packs_dir=tmp_path / "packs",
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
        )
        assert extras.prompt_files == []
        assert extras.mcp_config_path is None
        assert extras.env == {}


class TestPackCliExtrasWithPacks:
    @pytest.fixture()
    def setup(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        agents_dir.mkdir()
        packs_dir.mkdir()

        _write_agent_manifest(
            agents_dir / "sam" / "agent.yaml",
            """
            name: Sam
            container: sam
            packs: [github]
            """,
        )

        _write_pack(
            packs_dir,
            "github",
            manifest="""
            name: github
            description: GitHub via gh
            needs: [GITHUB_TOKEN]
            cli: gh
            """,
            files={
                "prompt.md": "# GitHub access\n",
                "mcp.json": json.dumps({"mcpServers": {"gh": {"command": "npx", "args": ["-y", "@gh/mcp"]}}}),
            },
        )
        return agents_dir, packs_dir

    def test_prompt_file_added(self, setup, tmp_path: Path) -> None:
        agents_dir, packs_dir = setup
        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
        )
        assert extras.prompt_files == [f"{CONTAINER_PACKS_DIR}/github/prompt.md"]

    def test_mcp_config_written(self, setup, tmp_path: Path) -> None:
        agents_dir, packs_dir = setup
        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
            tmp_dir=tmp_path,
        )
        assert extras.mcp_config_path is not None
        rendered = json.loads(Path(extras.mcp_config_path).read_text())
        assert "gh" in rendered["mcpServers"]
        assert rendered["mcpServers"]["gh"]["command"] == "npx"

    def test_env_injected_from_secret_store(self, setup, tmp_path: Path) -> None:
        agents_dir, packs_dir = setup
        store = SecretStore(path=tmp_path / "secrets.json")
        store.set("github", {"GITHUB_TOKEN": "ghp_test"})

        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=store,
        )
        assert extras.env == {"GITHUB_TOKEN": "ghp_test"}

    def test_env_case_insensitive_lookup(self, setup, tmp_path: Path) -> None:
        agents_dir, packs_dir = setup
        store = SecretStore(path=tmp_path / "secrets.json")
        store.set("github", {"github_token": "ghp_lower"})

        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=store,
        )
        assert extras.env == {"GITHUB_TOKEN": "ghp_lower"}

    def test_missing_secret_logged_not_raised(self, setup, tmp_path: Path) -> None:
        """A pack declaring needs but with no stored secrets should not crash."""
        agents_dir, packs_dir = setup
        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
        )
        assert extras.env == {}

    def test_unknown_pack_is_skipped(self, tmp_path: Path) -> None:
        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        packs_dir.mkdir()
        _write_agent_manifest(
            agents_dir / "sam" / "agent.yaml",
            """
            name: Sam
            packs: [ghost]
            """,
        )
        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
        )
        assert extras.prompt_files == []
        assert extras.mcp_config_path is None
        assert extras.env == {}


class TestMcpConfigMerging:
    def test_no_mcp_files_yields_no_config(self, tmp_path: Path) -> None:
        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        _write_agent_manifest(
            agents_dir / "sam" / "agent.yaml",
            """
            name: Sam
            packs: [thinpack]
            """,
        )
        _write_pack(packs_dir, "thinpack", manifest="name: thinpack")

        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
        )
        assert extras.mcp_config_path is None

    def test_two_packs_servers_merged(self, tmp_path: Path) -> None:
        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        _write_agent_manifest(
            agents_dir / "sam" / "agent.yaml",
            """
            name: Sam
            packs: [a, b]
            """,
        )
        _write_pack(
            packs_dir,
            "a",
            files={"mcp.json": json.dumps({"mcpServers": {"alpha": {"command": "a"}}})},
        )
        _write_pack(
            packs_dir,
            "b",
            files={"mcp.json": json.dumps({"mcpServers": {"beta": {"command": "b"}}})},
        )
        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
            tmp_dir=tmp_path,
        )
        cfg = json.loads(Path(extras.mcp_config_path).read_text())
        assert set(cfg["mcpServers"].keys()) == {"alpha", "beta"}


class TestSlackContextEnvInjection:
    """When an agent has the dispatch pack, inject DISPATCH_* env vars.

    Mirrors the GITHUB_TOKEN pattern: only agents that opt into the pack
    receive the env vars, and the dispatcher passes Slack context through
    so an agent can spawn follow-up dispatches without explicit flags.
    """

    def _setup_dispatch_agent(self, tmp_path: Path, *, extra_packs: list[str] = None):
        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        agents_dir.mkdir()
        packs_dir.mkdir()
        pack_list = ["dispatch"] + (extra_packs or [])
        _write_agent_manifest(
            agents_dir / "sam" / "agent.yaml",
            f"""
            name: Sam
            container: sam
            packs: {pack_list}
            """,
        )
        _write_pack(packs_dir, "dispatch", manifest="name: dispatch")
        return agents_dir, packs_dir

    def test_dispatch_pack_injects_all_three_env_vars(self, tmp_path: Path) -> None:
        agents_dir, packs_dir = self._setup_dispatch_agent(tmp_path)
        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
            channel="C123",
            thread_ts="1701234567.000100",
        )
        assert extras.env["DISPATCH_CHANNEL"] == "C123"
        assert extras.env["DISPATCH_THREAD_TS"] == "1701234567.000100"
        assert extras.env["DISPATCH_AGENT"] == "sam"

    def test_agent_without_dispatch_pack_gets_no_env(self, tmp_path: Path) -> None:
        """No leakage — same gating as GITHUB_TOKEN."""
        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        agents_dir.mkdir()
        packs_dir.mkdir()
        _write_agent_manifest(
            agents_dir / "lisa" / "agent.yaml",
            """
            name: Lisa
            container: lisa
            packs: [github]
            """,
        )
        _write_pack(packs_dir, "github", manifest="name: github")

        extras = pack_cli_extras(
            "lisa",
            manifest_path=agents_dir / "lisa" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
            channel="C123",
            thread_ts="1.0",
        )
        assert "DISPATCH_CHANNEL" not in extras.env
        assert "DISPATCH_THREAD_TS" not in extras.env
        assert "DISPATCH_AGENT" not in extras.env

    def test_dispatch_pack_without_context_skips_channel_thread(self, tmp_path: Path) -> None:
        """Host-side invocation (no Slack context) still works — only
        DISPATCH_AGENT (which the router always knows) gets populated."""
        agents_dir, packs_dir = self._setup_dispatch_agent(tmp_path)
        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
        )
        assert "DISPATCH_CHANNEL" not in extras.env
        assert "DISPATCH_THREAD_TS" not in extras.env
        assert extras.env["DISPATCH_AGENT"] == "sam"

    def test_dispatch_env_does_not_overwrite_secret_env(self, tmp_path: Path) -> None:
        """Co-existing with the github pack: DISPATCH_* and GITHUB_TOKEN
        both land in the same env dict without clobbering each other."""
        agents_dir, packs_dir = self._setup_dispatch_agent(tmp_path, extra_packs=["github"])
        _write_pack(
            packs_dir,
            "github",
            manifest="""
            name: github
            needs: [GITHUB_TOKEN]
            """,
        )
        store = SecretStore(path=tmp_path / "secrets.json")
        store.set("github", {"GITHUB_TOKEN": "ghp_x"})

        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=store,
            channel="C9",
            thread_ts="2.0",
        )
        assert extras.env["GITHUB_TOKEN"] == "ghp_x"
        assert extras.env["DISPATCH_CHANNEL"] == "C9"
        assert extras.env["DISPATCH_THREAD_TS"] == "2.0"
        assert extras.env["DISPATCH_AGENT"] == "sam"

    def test_slack_conversation_ref_injects_transport(self, tmp_path: Path) -> None:
        """#780: the named-agent responder path (router/chat/core.py) calls
        ``pack_cli_extras`` with only ``conversation_ref`` — no explicit
        ``channel``/``thread_ts`` kwargs. A ``slack:<channel>:<thread_ts>``
        ref must be decoded inline so DISPATCH_CHANNEL/DISPATCH_THREAD_TS
        still land, mirroring the existing ``discord:`` branch."""
        agents_dir, packs_dir = self._setup_dispatch_agent(tmp_path)

        # Ref-only: no explicit channel/thread_ts kwargs — decode the ref.
        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
            conversation_ref="slack:C123:1701234567.000100",
        )
        assert extras.env["DISPATCH_CHANNEL"] == "C123"
        assert extras.env["DISPATCH_THREAD_TS"] == "1701234567.000100"
        assert extras.env["DISPATCH_AGENT"] == "sam"

        # Explicit kwargs win over a conflicting ref.
        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
            channel="C-explicit",
            thread_ts="9.0",
            conversation_ref="slack:C123:1701234567.000100",
        )
        assert extras.env["DISPATCH_CHANNEL"] == "C-explicit"
        assert extras.env["DISPATCH_THREAD_TS"] == "9.0"

        # Malformed/short ref (missing thread_ts) — inject neither var.
        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
            conversation_ref="slack:C123",
        )
        assert "DISPATCH_CHANNEL" not in extras.env
        assert "DISPATCH_THREAD_TS" not in extras.env
        assert extras.env["DISPATCH_AGENT"] == "sam"

        # Discord path and non-dispatch-pack agents are unaffected.
        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
            conversation_ref="discord:123456789",
        )
        assert "DISPATCH_CHANNEL" not in extras.env
        assert "DISPATCH_THREAD_TS" not in extras.env
        assert extras.env["DISPATCH_TRANSPORT"] == "discord"
        assert extras.env["DISPATCH_CONVERSATION_ID"] == "discord:123456789"


class TestWorkersTokenInjection:
    """WORKERS_BOT_TOKEN is injected unconditionally — ``$WORKERS_BOT_TOKEN``
    (from .env) wins, falling back to the ``workers_bot_token`` secret-store
    entry. The store-path tests delenv the autouse fixture var to isolate that
    fallback; a dedicated test covers the env-wins precedence."""

    def _write_workers_token(self, path: Path, token: str) -> None:
        import json

        path.write_text(json.dumps({"workers_bot_token": token}))

    def test_injected_when_present_no_packs(self, tmp_path: Path) -> None:
        """WORKERS_BOT_TOKEN lands in env even when the agent has no packs."""
        manifest = tmp_path / "sam" / "agent.yaml"
        _write_agent_manifest(manifest, "name: Sam\ncontainer: sam")
        secrets_path = tmp_path / "secrets.json"
        self._write_workers_token(secrets_path, "xoxb-workers-test")

        extras = pack_cli_extras(
            "sam",
            manifest_path=manifest,
            packs_dir=tmp_path / "packs",
            secret_store=SecretStore(path=secrets_path),
        )
        assert extras.env["WORKERS_BOT_TOKEN"] == "xoxb-workers-test"

    def test_injected_when_present_with_packs(self, tmp_path: Path) -> None:
        """WORKERS_BOT_TOKEN and pack secrets coexist in env."""
        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        agents_dir.mkdir()
        packs_dir.mkdir()
        _write_agent_manifest(
            agents_dir / "sam" / "agent.yaml",
            "name: Sam\ncontainer: sam\npacks: [github]",
        )
        _write_pack(
            packs_dir,
            "github",
            manifest="name: github\nneeds: [GITHUB_TOKEN]",
        )
        secrets_path = tmp_path / "secrets.json"
        import json

        secrets_path.write_text(json.dumps({"workers_bot_token": "xoxb-workers", "github": {"GITHUB_TOKEN": "ghp_x"}}))
        store = SecretStore(path=secrets_path)

        extras = pack_cli_extras(
            "sam",
            manifest_path=agents_dir / "sam" / "agent.yaml",
            packs_dir=packs_dir,
            secret_store=store,
        )
        assert extras.env["WORKERS_BOT_TOKEN"] == "xoxb-workers"
        assert extras.env["GITHUB_TOKEN"] == "ghp_x"

    def test_missing_token_warns_not_crashes(self, tmp_path: Path, caplog, monkeypatch) -> None:
        """When workers_bot_token is absent the router warns and continues."""
        import logging

        monkeypatch.delenv("WORKERS_BOT_TOKEN", raising=False)
        manifest = tmp_path / "sam" / "agent.yaml"
        _write_agent_manifest(manifest, "name: Sam\ncontainer: sam")

        with caplog.at_level(logging.WARNING, logger="router.packs.dispatch_hook"):
            extras = pack_cli_extras(
                "sam",
                manifest_path=manifest,
                packs_dir=tmp_path / "packs",
                secret_store=SecretStore(path=tmp_path / "secrets.json"),
            )

        assert "WORKERS_BOT_TOKEN" not in extras.env
        assert any("workers_bot_token" in r.message for r in caplog.records)

    def test_missing_token_warns_not_crashes_with_packs(self, tmp_path: Path, caplog, monkeypatch) -> None:
        """Same clean-start guarantee when packs are present but token absent."""
        import logging

        monkeypatch.delenv("WORKERS_BOT_TOKEN", raising=False)
        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        agents_dir.mkdir()
        packs_dir.mkdir()
        _write_agent_manifest(
            agents_dir / "lisa" / "agent.yaml",
            "name: Lisa\ncontainer: lisa\npacks: [github]",
        )
        _write_pack(packs_dir, "github", manifest="name: github")

        with caplog.at_level(logging.WARNING, logger="router.packs.dispatch_hook"):
            extras = pack_cli_extras(
                "lisa",
                manifest_path=agents_dir / "lisa" / "agent.yaml",
                packs_dir=packs_dir,
                secret_store=SecretStore(path=tmp_path / "secrets.json"),
            )

        assert "WORKERS_BOT_TOKEN" not in extras.env
        assert any("workers_bot_token" in r.message for r in caplog.records)

    def test_store_wins_over_env_var(self, tmp_path: Path, monkeypatch) -> None:
        """The secrets.json entry (managed by the config page) takes precedence
        over $WORKERS_BOT_TOKEN from .env — store-over-env, #576. The env var
        remains a fallback for deployments that have not migrated."""
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-from-dotenv")
        manifest = tmp_path / "sam" / "agent.yaml"
        _write_agent_manifest(manifest, "name: Sam\ncontainer: sam")
        secrets_path = tmp_path / "secrets.json"
        self._write_workers_token(secrets_path, "xoxb-from-store")

        extras = pack_cli_extras(
            "sam",
            manifest_path=manifest,
            packs_dir=tmp_path / "packs",
            secret_store=SecretStore(path=secrets_path),
        )
        assert extras.env["WORKERS_BOT_TOKEN"] == "xoxb-from-store"

    def test_env_var_fallback_when_store_empty(self, tmp_path: Path, monkeypatch) -> None:
        """No store entry → the .env value is still honoured (migration fallback)."""
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-from-dotenv")
        manifest = tmp_path / "sam" / "agent.yaml"
        _write_agent_manifest(manifest, "name: Sam\ncontainer: sam")

        extras = pack_cli_extras(
            "sam",
            manifest_path=manifest,
            packs_dir=tmp_path / "packs",
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
        )
        assert extras.env["WORKERS_BOT_TOKEN"] == "xoxb-from-dotenv"

    def test_empty_env_var_falls_back_to_store(self, tmp_path: Path, monkeypatch) -> None:
        """An empty ``WORKERS_BOT_TOKEN`` (compose ``${VAR:-}`` with .env unset)
        is falsy → the store entry is still used. Guards the no-regression path."""
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "")
        manifest = tmp_path / "sam" / "agent.yaml"
        _write_agent_manifest(manifest, "name: Sam\ncontainer: sam")
        secrets_path = tmp_path / "secrets.json"
        self._write_workers_token(secrets_path, "xoxb-from-store")

        extras = pack_cli_extras(
            "sam",
            manifest_path=manifest,
            packs_dir=tmp_path / "packs",
            secret_store=SecretStore(path=secrets_path),
        )
        assert extras.env["WORKERS_BOT_TOKEN"] == "xoxb-from-store"


class TestWorkersDiscordTokenInjection:
    """WORKERS_DISCORD_TOKEN is injected on the Discord dispatch path only —
    sourced from the per-agent adapter bot token ``{AGENT}_DISCORD_BOT_TOKEN``
    (already a guild member, so never 403s).  Decision recorded in #680:
    the separate ``WORKERS_DISCORD_TOKEN`` identity is eliminated.
    Slack-path dispatches never receive it."""

    def _setup_dispatch_agent(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        agents_dir.mkdir()
        packs_dir.mkdir()
        _write_agent_manifest(
            agents_dir / "sam" / "agent.yaml",
            "name: Sam\ncontainer: sam\npacks: [dispatch]",
        )
        _write_pack(packs_dir, "dispatch", manifest="name: dispatch")
        return agents_dir / "sam" / "agent.yaml", packs_dir

    def test_per_agent_token_used_for_discord_path(self, tmp_path: Path, monkeypatch) -> None:
        """Discord path injects the per-agent bot token as WORKERS_DISCORD_TOKEN."""
        monkeypatch.setenv("SAM_DISCORD_BOT_TOKEN", "sam-discord-bot-token")
        manifest, packs_dir = self._setup_dispatch_agent(tmp_path)

        extras = pack_cli_extras(
            "sam",
            manifest_path=manifest,
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
            conversation_ref="discord:123:456:789",
        )
        assert extras.env["DISPATCH_TRANSPORT"] == "discord"
        assert extras.env["WORKERS_DISCORD_TOKEN"] == "sam-discord-bot-token"

    def test_missing_per_agent_token_warns_not_crashes(self, tmp_path: Path, monkeypatch, caplog) -> None:
        """When {AGENT}_DISCORD_BOT_TOKEN is absent, logs a warning and skips injection."""
        import logging

        monkeypatch.delenv("SAM_DISCORD_BOT_TOKEN", raising=False)
        manifest, packs_dir = self._setup_dispatch_agent(tmp_path)

        with caplog.at_level(logging.WARNING, logger="router.packs.dispatch_hook"):
            extras = pack_cli_extras(
                "sam",
                manifest_path=manifest,
                packs_dir=packs_dir,
                secret_store=SecretStore(path=tmp_path / "secrets.json"),
                conversation_ref="discord:123:456:789",
            )
        assert "WORKERS_DISCORD_TOKEN" not in extras.env
        assert any("SAM_DISCORD_BOT_TOKEN" in r.message for r in caplog.records)

    def test_slack_path_gets_no_discord_token(self, tmp_path: Path, monkeypatch) -> None:
        """Slack-path dispatches must never receive WORKERS_DISCORD_TOKEN."""
        monkeypatch.setenv("SAM_DISCORD_BOT_TOKEN", "sam-discord-bot-token")
        manifest, packs_dir = self._setup_dispatch_agent(tmp_path)

        extras = pack_cli_extras(
            "sam",
            manifest_path=manifest,
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
            channel="C123",
            thread_ts="1.0",
        )
        assert "WORKERS_DISCORD_TOKEN" not in extras.env
        assert "DISPATCH_TRANSPORT" not in extras.env


class TestDiscordWorkerStatusViaAgentFlag:
    """#707: DISCORD_WORKER_STATUS_VIA_AGENT routes worker status through the
    router's /internal/status callback instead of a direct Discord token.
    Flag-gated, default off — flag-off behaviour is byte-for-byte the
    pre-#707 path (covered by TestWorkersDiscordTokenInjection above)."""

    def _setup_dispatch_agent(self, tmp_path: Path):
        agents_dir = tmp_path / "agents"
        packs_dir = tmp_path / "packs"
        agents_dir.mkdir()
        packs_dir.mkdir()
        _write_agent_manifest(
            agents_dir / "sam" / "agent.yaml",
            "name: Sam\ncontainer: sam\npacks: [dispatch]",
        )
        _write_pack(packs_dir, "dispatch", manifest="name: dispatch")
        return agents_dir / "sam" / "agent.yaml", packs_dir

    @pytest.fixture()
    def hermetic_settings(self, tmp_path):
        """Isolated RuntimeSettings so setting the flag doesn't touch the real store."""
        instance = RuntimeSettings(
            path=tmp_path / "runtime.json",
            ttl=0.0,
            secret_store=SecretStore(path=tmp_path / "settings_secrets.json"),
        )
        settings_mod.reset_settings_for_tests(instance)
        yield instance
        settings_mod.reset_settings_for_tests(None)

    def test_flag_on_injects_marker_and_no_token(self, tmp_path: Path, monkeypatch, hermetic_settings) -> None:
        """Flag on: worker env gets DISCORD_WORKER_STATUS_VIA_AGENT=1, never a Discord token."""
        hermetic_settings.set("DISCORD_WORKER_STATUS_VIA_AGENT", True)
        monkeypatch.setenv("SAM_DISCORD_BOT_TOKEN", "sam-discord-bot-token")
        manifest, packs_dir = self._setup_dispatch_agent(tmp_path)

        extras = pack_cli_extras(
            "sam",
            manifest_path=manifest,
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
            conversation_ref="discord:123:456:789",
        )
        assert extras.env["DISPATCH_TRANSPORT"] == "discord"
        assert extras.env["DISCORD_WORKER_STATUS_VIA_AGENT"] == "1"
        assert "WORKERS_DISCORD_TOKEN" not in extras.env

    def test_flag_off_keeps_verbatim_token_path(self, tmp_path: Path, monkeypatch, hermetic_settings) -> None:
        """Flag explicitly off: identical to the pre-#707 per-agent-token path."""
        hermetic_settings.set("DISCORD_WORKER_STATUS_VIA_AGENT", False)
        monkeypatch.setenv("SAM_DISCORD_BOT_TOKEN", "sam-discord-bot-token")
        manifest, packs_dir = self._setup_dispatch_agent(tmp_path)

        extras = pack_cli_extras(
            "sam",
            manifest_path=manifest,
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
            conversation_ref="discord:123:456:789",
        )
        assert extras.env["WORKERS_DISCORD_TOKEN"] == "sam-discord-bot-token"
        assert "DISCORD_WORKER_STATUS_VIA_AGENT" not in extras.env

    def test_flag_on_slack_path_unaffected(self, tmp_path: Path, hermetic_settings) -> None:
        """Flag on but Slack-origin dispatch: no Discord-only keys leak in."""
        hermetic_settings.set("DISCORD_WORKER_STATUS_VIA_AGENT", True)
        manifest, packs_dir = self._setup_dispatch_agent(tmp_path)

        extras = pack_cli_extras(
            "sam",
            manifest_path=manifest,
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
            channel="C123",
            thread_ts="1.0",
        )
        assert "DISCORD_WORKER_STATUS_VIA_AGENT" not in extras.env
        assert "WORKERS_DISCORD_TOKEN" not in extras.env
        assert "DISPATCH_TRANSPORT" not in extras.env
