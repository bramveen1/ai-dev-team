"""Unit tests for issue #663: de-Slack the dispatch surface via TransportRef.

Acceptance criteria verified:
1. ``TransportRef`` type + resolver exist; ``handler.py`` consumes them.
2. Slack path is byte-for-byte equivalent to ``_resolve_slack_context``
   (regression guard).
3. Discord-origin ``dispatch.draft`` no longer raises ``missing_slack_context``
   or ``missing_transport_context``; it proceeds to the draft-creation step.
4. Unknown transport → fail-closed (ValueError from resolver).
5. ``_post_fn`` injectable in ``_acquire_slot`` / ``dispatch_issue`` routes
   status to the non-Slack transport.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "packs" / "dispatch"


def _load_handler():
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location(
        "_test_pack_dispatch_663_handler",
        PACK_DIR / "handler.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_transport_ref():
    if str(PACK_DIR) not in sys.path:
        sys.path.insert(0, str(PACK_DIR))
    spec = importlib.util.spec_from_file_location(
        "_test_pack_transport_ref_663",
        PACK_DIR / "transport_ref.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def handler():
    return _load_handler()


@pytest.fixture
def tref_mod():
    return _load_transport_ref()


# ---------------------------------------------------------------------------
# TransportRef module
# ---------------------------------------------------------------------------


class TestTransportRefModule:
    def test_transport_ref_importable(self, tref_mod):
        assert hasattr(tref_mod, "TransportRef")
        assert hasattr(tref_mod, "resolve_transport_ref")
        assert hasattr(tref_mod, "_post_discord_message")

    def test_transport_ref_dataclass_fields(self, tref_mod):
        ref = tref_mod.TransportRef(
            transport="slack",
            conversation_id="C123",
            thread_id="1234.5678",
            agent="sam",
        )
        assert ref.transport == "slack"
        assert ref.conversation_id == "C123"
        assert ref.thread_id == "1234.5678"
        assert ref.agent == "sam"


# ---------------------------------------------------------------------------
# resolve_transport_ref — Slack path (regression guard)
# ---------------------------------------------------------------------------


class TestResolveTransportRefSlack:
    """Slack path must be byte-for-byte equivalent to _resolve_slack_context."""

    def test_slack_from_env(self, tref_mod, monkeypatch):
        monkeypatch.setenv("DISPATCH_CHANNEL", "C999")
        monkeypatch.setenv("DISPATCH_THREAD_TS", "1700000000.000001")
        monkeypatch.setenv("DISPATCH_AGENT", "sam")
        monkeypatch.delenv("DISPATCH_TRANSPORT", raising=False)

        ref = tref_mod.resolve_transport_ref()
        assert ref.transport == "slack"
        assert ref.conversation_id == "C999"
        assert ref.thread_id == "1700000000.000001"
        assert ref.agent == "sam"

    def test_slack_flag_wins_over_env(self, tref_mod, monkeypatch):
        monkeypatch.setenv("DISPATCH_CHANNEL", "C_env")
        monkeypatch.setenv("DISPATCH_THREAD_TS", "111.000")
        monkeypatch.setenv("DISPATCH_AGENT", "env-agent")

        ref = tref_mod.resolve_transport_ref(
            channel="C_flag",
            thread_ts="222.000",
            agent="flag-agent",
        )
        assert ref.conversation_id == "C_flag"
        assert ref.thread_id == "222.000"
        assert ref.agent == "flag-agent"

    def test_slack_missing_channel_raises(self, tref_mod, monkeypatch):
        monkeypatch.delenv("DISPATCH_CHANNEL", raising=False)
        monkeypatch.setenv("DISPATCH_THREAD_TS", "111.000")
        monkeypatch.setenv("DISPATCH_AGENT", "sam")
        monkeypatch.delenv("DISPATCH_TRANSPORT", raising=False)

        with pytest.raises(ValueError, match="DISPATCH_CHANNEL"):
            tref_mod.resolve_transport_ref()

    def test_slack_missing_thread_ts_raises(self, tref_mod, monkeypatch):
        monkeypatch.setenv("DISPATCH_CHANNEL", "C123")
        monkeypatch.delenv("DISPATCH_THREAD_TS", raising=False)
        monkeypatch.setenv("DISPATCH_AGENT", "sam")
        monkeypatch.delenv("DISPATCH_TRANSPORT", raising=False)

        with pytest.raises(ValueError, match="DISPATCH_THREAD_TS"):
            tref_mod.resolve_transport_ref()

    def test_slack_missing_agent_raises(self, tref_mod, monkeypatch):
        monkeypatch.setenv("DISPATCH_CHANNEL", "C123")
        monkeypatch.setenv("DISPATCH_THREAD_TS", "111.000")
        monkeypatch.delenv("DISPATCH_AGENT", raising=False)
        monkeypatch.delenv("DISPATCH_TRANSPORT", raising=False)

        with pytest.raises(ValueError, match="DISPATCH_AGENT"):
            tref_mod.resolve_transport_ref()

    def test_slack_path_explicitly_set(self, tref_mod, monkeypatch):
        ref = tref_mod.resolve_transport_ref(
            transport="slack",
            channel="C_explicit",
            thread_ts="333.000",
            agent="lisa",
        )
        assert ref.transport == "slack"
        assert ref.conversation_id == "C_explicit"

    def test_slack_regression_resolve_slack_context_equivalent(self, handler, monkeypatch):
        """Slack path through _resolve_slack_context and resolve_transport_ref
        must produce identical channel / thread_ts / agent."""
        monkeypatch.setenv("DISPATCH_CHANNEL", "C_abc")
        monkeypatch.setenv("DISPATCH_THREAD_TS", "9999.000001")
        monkeypatch.setenv("DISPATCH_AGENT", "sam")
        monkeypatch.delenv("DISPATCH_TRANSPORT", raising=False)

        old_ch, old_ts, old_ag = handler._resolve_slack_context(channel=None, thread_ts=None, agent=None)
        new_ref = handler._resolve_transport_context(channel=None, thread_ts=None, agent=None)
        new_ch, new_ts, new_ag, _ = handler._unpack_transport_ref(new_ref)

        assert old_ch == new_ch == "C_abc"
        assert old_ts == new_ts == "9999.000001"
        assert old_ag == new_ag == "sam"


# ---------------------------------------------------------------------------
# resolve_transport_ref — Discord path
# ---------------------------------------------------------------------------


class TestResolveTransportRefDiscord:
    def test_discord_from_env(self, tref_mod, monkeypatch):
        monkeypatch.setenv("DISPATCH_TRANSPORT", "discord")
        monkeypatch.setenv("DISPATCH_CONVERSATION_ID", "discord:123:456:789")
        monkeypatch.setenv("DISPATCH_AGENT", "sam")

        ref = tref_mod.resolve_transport_ref()
        assert ref.transport == "discord"
        assert ref.conversation_id == "discord:123:456:789"
        assert ref.thread_id == ""
        assert ref.agent == "sam"

    def test_discord_missing_conversation_id_raises(self, tref_mod, monkeypatch):
        monkeypatch.setenv("DISPATCH_TRANSPORT", "discord")
        monkeypatch.delenv("DISPATCH_CONVERSATION_ID", raising=False)
        monkeypatch.setenv("DISPATCH_AGENT", "sam")

        with pytest.raises(ValueError, match="DISPATCH_CONVERSATION_ID"):
            tref_mod.resolve_transport_ref()

    def test_discord_missing_agent_raises(self, tref_mod, monkeypatch):
        monkeypatch.setenv("DISPATCH_TRANSPORT", "discord")
        monkeypatch.setenv("DISPATCH_CONVERSATION_ID", "discord:1:2:3")
        monkeypatch.delenv("DISPATCH_AGENT", raising=False)

        with pytest.raises(ValueError, match="DISPATCH_AGENT"):
            tref_mod.resolve_transport_ref()


# ---------------------------------------------------------------------------
# resolve_transport_ref — terminal path
# ---------------------------------------------------------------------------


class TestResolveTransportRefTerminal:
    def test_terminal_from_env(self, tref_mod, monkeypatch):
        monkeypatch.setenv("DISPATCH_TRANSPORT", "terminal")
        monkeypatch.setenv("DISPATCH_AGENT", "sam")

        ref = tref_mod.resolve_transport_ref()
        assert ref.transport == "terminal"
        assert ref.conversation_id == ""
        assert ref.thread_id == ""
        assert ref.agent == "sam"


# ---------------------------------------------------------------------------
# resolve_transport_ref — unknown transport (fail-closed)
# ---------------------------------------------------------------------------


class TestResolveTransportRefUnknown:
    def test_unknown_transport_raises(self, tref_mod, monkeypatch):
        monkeypatch.setenv("DISPATCH_TRANSPORT", "fax")
        monkeypatch.setenv("DISPATCH_AGENT", "sam")

        with pytest.raises(ValueError, match="unknown transport"):
            tref_mod.resolve_transport_ref()

    def test_blank_transport_defaults_to_slack(self, tref_mod, monkeypatch):
        monkeypatch.setenv("DISPATCH_TRANSPORT", "")
        monkeypatch.setenv("DISPATCH_CHANNEL", "C1")
        monkeypatch.setenv("DISPATCH_THREAD_TS", "111.1")
        monkeypatch.setenv("DISPATCH_AGENT", "sam")

        ref = tref_mod.resolve_transport_ref()
        assert ref.transport == "slack"


# ---------------------------------------------------------------------------
# handler._resolve_transport_context integration
# ---------------------------------------------------------------------------


class TestHandlerResolveTransportContext:
    def test_slack_env_resolves_cleanly(self, handler, monkeypatch):
        monkeypatch.setenv("DISPATCH_CHANNEL", "C_handler")
        monkeypatch.setenv("DISPATCH_THREAD_TS", "1000.001")
        monkeypatch.setenv("DISPATCH_AGENT", "sam")
        monkeypatch.delenv("DISPATCH_TRANSPORT", raising=False)

        tref = handler._resolve_transport_context()
        channel, thread_ts, agent, post_fn = handler._unpack_transport_ref(tref)
        assert channel == "C_handler"
        assert thread_ts == "1000.001"
        assert agent == "sam"
        assert post_fn is None  # Slack path uses _post_slack_message directly

    def test_discord_env_resolves_cleanly(self, handler, monkeypatch):
        monkeypatch.setenv("DISPATCH_TRANSPORT", "discord")
        monkeypatch.setenv("DISPATCH_CONVERSATION_ID", "discord:100:200:300")
        monkeypatch.setenv("DISPATCH_AGENT", "sam")

        tref = handler._resolve_transport_context()
        channel, thread_ts, agent, post_fn = handler._unpack_transport_ref(tref)
        assert channel == ""
        assert thread_ts == ""
        assert agent == "sam"
        assert post_fn is not None  # Discord path uses injectable post_fn

    def test_discord_post_fn_is_callable(self, handler, monkeypatch):
        monkeypatch.setenv("DISPATCH_TRANSPORT", "discord")
        monkeypatch.setenv("DISPATCH_CONVERSATION_ID", "discord:1:2:3")
        monkeypatch.setenv("DISPATCH_AGENT", "sam")

        tref = handler._resolve_transport_context()
        _, _, _, post_fn = handler._unpack_transport_ref(tref)
        assert callable(post_fn)

    def test_unknown_transport_raises(self, handler, monkeypatch):
        monkeypatch.setenv("DISPATCH_TRANSPORT", "telepathy")
        monkeypatch.setenv("DISPATCH_AGENT", "sam")

        with pytest.raises(ValueError, match="unknown transport"):
            handler._resolve_transport_context()


# ---------------------------------------------------------------------------
# dispatch.draft CLI verb — Discord no longer raises missing_slack_context
# ---------------------------------------------------------------------------


class TestDispatchDraftDiscordNoMissingSlackContext:
    """AC3: Discord-origin dispatch.draft no longer raises missing_slack_context."""

    def _make_fake_urlopen(self, response_body: bytes):
        """Return a _urlopen fake that returns a response with the given body."""

        class _FakeResp:
            def read(self):
                return response_body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        def _opener(req, timeout=None):  # noqa: ARG001
            return _FakeResp()

        return _opener

    def test_dispatch_draft_with_discord_transport_resolves(self, handler, monkeypatch, tmp_path):
        """dispatch.draft with DISPATCH_TRANSPORT=discord must not raise
        missing_slack_context / missing_transport_context."""
        monkeypatch.setenv("DISPATCH_TRANSPORT", "discord")
        monkeypatch.setenv("DISPATCH_CONVERSATION_ID", "discord:100:200:300")
        monkeypatch.setenv("DISPATCH_AGENT", "sam")
        monkeypatch.delenv("DISPATCH_CHANNEL", raising=False)
        monkeypatch.delenv("DISPATCH_THREAD_TS", raising=False)

        # Mock the router call — returns a valid draft_created response.
        fake_response = json.dumps({"draft_id": "abc123", "status": "draft_created", "card_ts": "1700.0"}).encode()

        result = handler.dispatch_draft(
            issue_url="https://github.com/owner/repo/issues/1",
            channel="",
            thread_ts="",
            agent="sam",
            transport="discord",
            conversation_id="discord:100:200:300",
            _approval_cfg={"require_always": False, "destructive_keywords": []},
            _urlopen=self._make_fake_urlopen(fake_response),
            _router_url="http://router:8090/internal/drafts",
        )
        # Result must not be an error about missing Slack context.
        assert result.get("status") != "error" or result.get("reason") not in (
            "missing_slack_context",
            "missing_transport_context",
        )

    def test_dispatch_draft_with_slack_still_works(self, handler, monkeypatch):
        """Slack path remains unchanged after the discord changes."""
        monkeypatch.delenv("DISPATCH_TRANSPORT", raising=False)

        fake_response = json.dumps({"draft_id": "def456", "status": "draft_created", "card_ts": "1800.0"}).encode()

        result = handler.dispatch_draft(
            issue_url="https://github.com/owner/repo/issues/2",
            channel="C_slack",
            thread_ts="9999.000001",
            agent="sam",
            _approval_cfg={"require_always": False, "destructive_keywords": []},
            _urlopen=lambda req, timeout=None: type(
                "_R",
                (),
                {"read": lambda self: fake_response, "__enter__": lambda self: self, "__exit__": lambda *a: None},
            )(),
            _router_url="http://router:8090/internal/drafts",
        )
        # We only care that Slack transport resolution didn't fail.
        # The call may still error on missing_token / network in CI.
        assert result.get("status") != "error" or result.get("reason") not in (
            "missing_slack_context",
            "missing_transport_context",
        )


# ---------------------------------------------------------------------------
# dispatch.draft CLI verb — missing context errors properly reported
# ---------------------------------------------------------------------------


class TestDispatchDraftCliMissingContext:
    def test_cli_dispatch_draft_missing_discord_context_errors(self, handler, capsys, monkeypatch):
        """dispatch.draft with DISPATCH_TRANSPORT=discord but no conversation_id → clean error."""
        monkeypatch.setenv("DISPATCH_TRANSPORT", "discord")
        monkeypatch.delenv("DISPATCH_CONVERSATION_ID", raising=False)
        monkeypatch.setenv("DISPATCH_AGENT", "sam")

        rc = handler.run(
            [
                "dispatch.draft",
                "--issue-url",
                "https://github.com/o/r/issues/7",
            ]
        )
        assert rc != 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"] == "missing_transport_context"
        assert "DISPATCH_CONVERSATION_ID" in payload["message"]

    def test_cli_dispatch_draft_unknown_transport_errors(self, handler, capsys, monkeypatch):
        """dispatch.draft with an unknown transport → clean error."""
        monkeypatch.setenv("DISPATCH_TRANSPORT", "fax")
        monkeypatch.setenv("DISPATCH_AGENT", "sam")

        rc = handler.run(
            [
                "dispatch.draft",
                "--issue-url",
                "https://github.com/o/r/issues/8",
            ]
        )
        assert rc != 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"] == "missing_transport_context"
        assert "unknown transport" in payload["message"]


# ---------------------------------------------------------------------------
# _post_fn injectable in dispatch_issue (Discord degrade-not-crash)
# ---------------------------------------------------------------------------


class TestDispatchIssuePostFnInjectable:
    """AC5: when _post_fn is set, status messages go through it; no crash on failure."""

    _NO_GATE_CFG = {"require_always": False, "destructive_keywords": []}

    def _no_op_seed_auth(self, workspace: Path) -> Path:
        d = workspace / "auth"
        d.mkdir(exist_ok=True)
        return d

    def test_post_fn_called_on_auth_seed_failure(self, handler, tmp_path):
        """When auth seed fails, _post_fn receives the error message (not _post_slack_message)."""
        calls: list[str] = []

        def _capture_post(msg: str) -> None:
            calls.append(msg)

        def _fail_seed(workspace: Path) -> Path:
            raise RuntimeError("creds missing")

        result = handler.dispatch_issue(
            issue_url="https://github.com/o/r/issues/10",
            channel="",
            thread_ts="",
            agent="sam",
            exec_override=None,
            workspace_root=tmp_path,
            _seed_auth_fn=_fail_seed,
            _approval_cfg=self._NO_GATE_CFG,
            _post_fn=_capture_post,
            _dispatch_token_path=None,
        )
        assert result["status"] == "error"
        assert result["reason"] == "auth_seed_failed"
        assert len(calls) == 1
        assert "auth_seed_failed" in calls[0]

    def test_post_fn_exception_does_not_crash_dispatch(self, handler, tmp_path):
        """If _post_fn raises, dispatch continues (degrade-not-crash contract)."""

        def _raise_post(msg: str) -> None:
            raise RuntimeError("Discord unavailable")

        def _fail_seed(workspace: Path) -> Path:
            raise RuntimeError("creds missing")

        result = handler.dispatch_issue(
            issue_url="https://github.com/o/r/issues/11",
            channel="",
            thread_ts="",
            agent="sam",
            exec_override=None,
            workspace_root=tmp_path,
            _seed_auth_fn=_fail_seed,
            _approval_cfg=self._NO_GATE_CFG,
            _post_fn=_raise_post,
            _dispatch_token_path=None,
        )
        # Must complete with an error dict, not an uncaught exception.
        assert result["status"] == "error"
        assert result["reason"] == "auth_seed_failed"


# ---------------------------------------------------------------------------
# dispatch_hook.py — Discord context injection
# ---------------------------------------------------------------------------


class TestPackCliExtrasDiscord:
    """pack_cli_extras injects DISPATCH_TRANSPORT + DISPATCH_CONVERSATION_ID
    when conversation_ref starts with 'discord:'."""

    def _write_agent_yaml(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def _write_pack(self, packs_dir: Path, name: str) -> None:
        pack_dir = packs_dir / name
        pack_dir.mkdir(parents=True)
        (pack_dir / "pack.yaml").write_text(f"name: {name}\n")

    def test_discord_conv_ref_injects_transport_vars(self, tmp_path):
        from router.packs.dispatch_hook import pack_cli_extras
        from router.packs.secret_store import SecretStore

        manifest = tmp_path / "sam" / "agent.yaml"
        self._write_agent_yaml(manifest, "name: Sam\ncontainer: sam\npacks: [dispatch]\n")
        packs_dir = tmp_path / "packs"
        self._write_pack(packs_dir, "dispatch")

        extras = pack_cli_extras(
            "sam",
            manifest_path=manifest,
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
            conversation_ref="discord:100:200:300",
        )
        assert extras.env.get("DISPATCH_TRANSPORT") == "discord"
        assert extras.env.get("DISPATCH_CONVERSATION_ID") == "discord:100:200:300"
        assert extras.env.get("DISPATCH_AGENT") == "sam"
        # Slack vars must NOT be injected for Discord dispatches.
        assert "DISPATCH_CHANNEL" not in extras.env
        assert "DISPATCH_THREAD_TS" not in extras.env

    def test_slack_conv_ref_injects_old_triple(self, tmp_path):
        from router.packs.dispatch_hook import pack_cli_extras
        from router.packs.secret_store import SecretStore

        manifest = tmp_path / "sam" / "agent.yaml"
        self._write_agent_yaml(manifest, "name: Sam\ncontainer: sam\npacks: [dispatch]\n")
        packs_dir = tmp_path / "packs"
        self._write_pack(packs_dir, "dispatch")

        extras = pack_cli_extras(
            "sam",
            manifest_path=manifest,
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
            channel="C_slack",
            thread_ts="9999.000001",
        )
        assert extras.env.get("DISPATCH_CHANNEL") == "C_slack"
        assert extras.env.get("DISPATCH_THREAD_TS") == "9999.000001"
        assert extras.env.get("DISPATCH_AGENT") == "sam"
        assert "DISPATCH_TRANSPORT" not in extras.env
        assert "DISPATCH_CONVERSATION_ID" not in extras.env

    def test_no_conversation_ref_no_channel_still_sets_agent(self, tmp_path):
        from router.packs.dispatch_hook import pack_cli_extras
        from router.packs.secret_store import SecretStore

        manifest = tmp_path / "sam" / "agent.yaml"
        self._write_agent_yaml(manifest, "name: Sam\ncontainer: sam\npacks: [dispatch]\n")
        packs_dir = tmp_path / "packs"
        self._write_pack(packs_dir, "dispatch")

        extras = pack_cli_extras(
            "sam",
            manifest_path=manifest,
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
        )
        assert extras.env.get("DISPATCH_AGENT") == "sam"

    def test_discord_token_injected_from_per_agent_env(self, tmp_path, monkeypatch):
        from router.packs.dispatch_hook import pack_cli_extras
        from router.packs.secret_store import SecretStore

        manifest = tmp_path / "sam" / "agent.yaml"
        self._write_agent_yaml(manifest, "name: Sam\ncontainer: sam\npacks: [dispatch]\n")
        packs_dir = tmp_path / "packs"
        self._write_pack(packs_dir, "dispatch")

        monkeypatch.setenv("SAM_DISCORD_BOT_TOKEN", "sam-discord-bot-xyz")

        extras = pack_cli_extras(
            "sam",
            manifest_path=manifest,
            packs_dir=packs_dir,
            secret_store=SecretStore(path=tmp_path / "secrets.json"),
            conversation_ref="discord:1:2:3",
        )
        assert extras.env.get("WORKERS_DISCORD_TOKEN") == "sam-discord-bot-xyz"
