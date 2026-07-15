"""Unit tests for issue #674: dispatch CLI ``--transport`` / ``--conversation-id``
flags on ``dispatch_issue`` and ``dispatch.draft``.

Prior to this change the raw CLI only exposed ``--channel`` / ``--thread-ts`` /
``--agent`` and relied entirely on ``$DISPATCH_TRANSPORT`` / ``$DISPATCH_CONVERSATION_ID``
env fallback (see #713) for non-Slack routing. That left a self-dispatch call
made explicitly with a Discord ref (rather than relying on ambient env) with
no CLI surface to supply it.

Acceptance criteria verified:
1. ``dispatch_issue`` and ``dispatch.draft`` accept ``--transport`` /
   ``--conversation-id`` flags.
2. Flags reach the underlying ``dispatch_issue`` / ``dispatch_draft`` calls
   (and, from there, the sidecar state / draft payload the approve->execute
   path already threads through, per #665/#669/#713).
3. Flags win over env (explicit CLI override of ambient DISPATCH_* env).
4. Slack path (no flags, no non-Slack env) is unchanged — regression guard.
5. Missing Discord conversation-id / unknown transport surface a clear
   ``missing_transport_context`` error, no crash.
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
        "_test_pack_dispatch_674_handler",
        PACK_DIR / "handler.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def handler():
    return _load_handler()


# ---------------------------------------------------------------------------
# dispatch_issue CLI verb
# ---------------------------------------------------------------------------


class TestDispatchIssueCliTransportFlags:
    def test_discord_flags_thread_through(self, handler, tmp_path: Path, capsys, monkeypatch) -> None:
        recorded: dict = {}

        def fake_dispatch_issue(**kwargs):
            recorded.update(kwargs)
            return {"status": "launched", "dispatch_id": "disp-1", "workspace": str(tmp_path), "pid": 1}

        monkeypatch.setattr(handler, "dispatch_issue", fake_dispatch_issue)
        monkeypatch.delenv("DISPATCH_TRANSPORT", raising=False)
        monkeypatch.delenv("DISPATCH_CONVERSATION_ID", raising=False)

        rc = handler.run(
            [
                "dispatch_issue",
                "--issue-url",
                "https://github.com/o/r/issues/1",
                "--agent",
                "sam",
                "--transport",
                "discord",
                "--conversation-id",
                "discord:1:2:3",
            ]
        )
        assert rc == 0
        assert recorded["transport"] == "discord"
        assert recorded["conversation_id"] == "discord:1:2:3"

    def test_flags_win_over_env(self, handler, tmp_path: Path, monkeypatch) -> None:
        """Explicit --transport/--conversation-id override ambient DISPATCH_* env."""
        recorded: dict = {}

        def fake_dispatch_issue(**kwargs):
            recorded.update(kwargs)
            return {"status": "launched", "dispatch_id": "disp-2", "workspace": str(tmp_path), "pid": 2}

        monkeypatch.setattr(handler, "dispatch_issue", fake_dispatch_issue)
        monkeypatch.setenv("DISPATCH_TRANSPORT", "discord")
        monkeypatch.setenv("DISPATCH_CONVERSATION_ID", "discord:9:9:9")
        monkeypatch.setenv("DISPATCH_AGENT", "sam")

        rc = handler.run(
            [
                "dispatch_issue",
                "--issue-url",
                "https://github.com/o/r/issues/2",
                "--transport",
                "terminal",
            ]
        )
        assert rc == 0
        assert recorded["transport"] == "terminal"
        assert recorded["conversation_id"] == ""

    def test_slack_regression_no_flags(self, handler, tmp_path: Path, monkeypatch) -> None:
        """No --transport/--conversation-id, no non-Slack env → slack, unchanged."""
        recorded: dict = {}

        def fake_dispatch_issue(**kwargs):
            recorded.update(kwargs)
            return {"status": "launched", "dispatch_id": "disp-3", "workspace": str(tmp_path), "pid": 3}

        monkeypatch.setattr(handler, "dispatch_issue", fake_dispatch_issue)
        monkeypatch.delenv("DISPATCH_TRANSPORT", raising=False)
        monkeypatch.delenv("DISPATCH_CONVERSATION_ID", raising=False)

        rc = handler.run(
            [
                "dispatch_issue",
                "--issue-url",
                "https://github.com/o/r/issues/3",
                "--channel",
                "C1",
                "--thread-ts",
                "1.0",
                "--agent",
                "sam",
            ]
        )
        assert rc == 0
        assert recorded["channel"] == "C1"
        assert recorded["thread_ts"] == "1.0"
        assert recorded["transport"] == "slack"
        assert recorded["conversation_id"] == ""

    def test_discord_transport_missing_conversation_id_errors(self, handler, capsys, monkeypatch) -> None:
        monkeypatch.delenv("DISPATCH_CONVERSATION_ID", raising=False)

        rc = handler.run(
            [
                "dispatch_issue",
                "--issue-url",
                "https://github.com/o/r/issues/4",
                "--agent",
                "sam",
                "--transport",
                "discord",
            ]
        )
        assert rc != 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert payload["reason"] == "missing_transport_context"
        assert "conversation-id" in payload["detail"] or "DISPATCH_CONVERSATION_ID" in payload["detail"]

    def test_unknown_transport_errors(self, handler, capsys, monkeypatch) -> None:
        rc = handler.run(
            [
                "dispatch_issue",
                "--issue-url",
                "https://github.com/o/r/issues/5",
                "--agent",
                "sam",
                "--transport",
                "carrier-pigeon",
            ]
        )
        assert rc != 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert payload["reason"] == "missing_transport_context"
        assert "unknown transport" in payload["detail"]


# ---------------------------------------------------------------------------
# dispatch.draft CLI verb
# ---------------------------------------------------------------------------


class TestDispatchDraftCliTransportFlags:
    def test_discord_flags_thread_through(self, handler, monkeypatch) -> None:
        recorded: dict = {}

        def fake_dispatch_draft(**kwargs):
            recorded.update(kwargs)
            return {"status": "draft_created", "draft_id": "d-1", "gate_reason": "", "card_ts": "1.0"}

        monkeypatch.setattr(handler, "dispatch_draft", fake_dispatch_draft)
        monkeypatch.delenv("DISPATCH_TRANSPORT", raising=False)
        monkeypatch.delenv("DISPATCH_CONVERSATION_ID", raising=False)

        rc = handler.run(
            [
                "dispatch.draft",
                "--issue-url",
                "https://github.com/o/r/issues/6",
                "--agent",
                "sam",
                "--transport",
                "discord",
                "--conversation-id",
                "discord:4:5:6",
            ]
        )
        assert rc == 0
        assert recorded["transport"] == "discord"
        assert recorded["conversation_id"] == "discord:4:5:6"

    def test_slack_regression_no_flags(self, handler, monkeypatch) -> None:
        recorded: dict = {}

        def fake_dispatch_draft(**kwargs):
            recorded.update(kwargs)
            return {"status": "draft_created", "draft_id": "d-2", "gate_reason": "", "card_ts": "1.0"}

        monkeypatch.setattr(handler, "dispatch_draft", fake_dispatch_draft)
        monkeypatch.delenv("DISPATCH_TRANSPORT", raising=False)
        monkeypatch.delenv("DISPATCH_CONVERSATION_ID", raising=False)

        rc = handler.run(
            [
                "dispatch.draft",
                "--issue-url",
                "https://github.com/o/r/issues/7",
                "--channel",
                "C_slack",
                "--thread-ts",
                "2.0",
                "--agent",
                "sam",
            ]
        )
        assert rc == 0
        assert recorded["channel"] == "C_slack"
        assert recorded["thread_ts"] == "2.0"
        assert recorded["transport"] == "slack"

    def test_missing_discord_conversation_id_degrades_to_clear_error(self, handler, capsys, monkeypatch) -> None:
        """Missing flags/env for a declared discord transport → clean error,
        not a crash (degrade-not-crash contract, no silent Slack fallback)."""
        monkeypatch.delenv("DISPATCH_CONVERSATION_ID", raising=False)

        rc = handler.run(
            [
                "dispatch.draft",
                "--issue-url",
                "https://github.com/o/r/issues/8",
                "--agent",
                "sam",
                "--transport",
                "discord",
            ]
        )
        assert rc != 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"] == "missing_transport_context"

    def test_unknown_transport_errors(self, handler, capsys, monkeypatch) -> None:
        rc = handler.run(
            [
                "dispatch.draft",
                "--issue-url",
                "https://github.com/o/r/issues/9",
                "--agent",
                "sam",
                "--transport",
                "smoke-signal",
            ]
        )
        assert rc != 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"] == "missing_transport_context"
        assert "unknown transport" in payload["message"]


# ---------------------------------------------------------------------------
# _resolve_transport_context — direct unit coverage of the new params
# ---------------------------------------------------------------------------


class TestResolveTransportContextFlags:
    def test_transport_and_conversation_id_flags_resolve_discord(self, handler, monkeypatch) -> None:
        monkeypatch.delenv("DISPATCH_TRANSPORT", raising=False)
        monkeypatch.delenv("DISPATCH_CONVERSATION_ID", raising=False)

        tref = handler._resolve_transport_context(
            agent="sam",
            transport="discord",
            conversation_id="discord:7:8:9",
        )
        assert tref.transport == "discord"
        assert tref.conversation_id == "discord:7:8:9"
        assert tref.agent == "sam"

    def test_flags_win_over_env(self, handler, monkeypatch) -> None:
        monkeypatch.setenv("DISPATCH_TRANSPORT", "discord")
        monkeypatch.setenv("DISPATCH_CONVERSATION_ID", "discord:0:0:0")
        monkeypatch.setenv("DISPATCH_AGENT", "sam")

        tref = handler._resolve_transport_context(transport="terminal")
        assert tref.transport == "terminal"

    def test_env_fallback_when_flags_absent(self, handler, monkeypatch) -> None:
        monkeypatch.setenv("DISPATCH_TRANSPORT", "discord")
        monkeypatch.setenv("DISPATCH_CONVERSATION_ID", "discord:1:1:1")
        monkeypatch.setenv("DISPATCH_AGENT", "sam")

        tref = handler._resolve_transport_context()
        assert tref.transport == "discord"
        assert tref.conversation_id == "discord:1:1:1"
