"""Tests for router.commands.slack_shim — the Slack-specific entry shim.

Covers:
* strip_mention / strip_aidt helpers
* parse_slack_slash: slash body → Command (verb, scope, args, transport)
* parse_from_message: @mention or aidt text → Command
* Killall migration: /kill all → killall (global)
* Unknown verb → None (defensive fallback)
* Context fields passed through to Command
* Smoke probes: kill (agent-scoped) + killall (global) through the full
  shim → parser → handler path asserting the right handler fires with the
  right subject.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router.commands.slack_shim import (
    parse_from_message,
    parse_slack_slash,
    strip_aidt,
    strip_mention,
)
from router.commands.types import SCOPE_AGENT, SCOPE_GLOBAL

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# strip_mention
# ---------------------------------------------------------------------------


class TestStripMention:
    def test_strips_mention_prefix(self):
        assert strip_mention("<@U123ABC> kill") == "kill"

    def test_strips_mention_with_extra_whitespace(self):
        assert strip_mention("<@U123ABC>   grant sam github") == "grant sam github"

    def test_no_mention_unchanged(self):
        assert strip_mention("grant sam github") == "grant sam github"

    def test_empty_string(self):
        assert strip_mention("") == ""

    def test_bare_mention(self):
        assert strip_mention("<@U123ABC>") == ""

    def test_lowercase_mention_chars_also_stripped(self):
        # re.IGNORECASE makes the pattern match lowercase IDs too.
        assert strip_mention("<@u123abc> kill") == "kill"


# ---------------------------------------------------------------------------
# strip_aidt
# ---------------------------------------------------------------------------


class TestStripAidt:
    def test_strips_aidt_keyword(self):
        assert strip_aidt("aidt kill") == "kill"

    def test_strips_aidt_case_insensitive(self):
        assert strip_aidt("AIDT kill") == "kill"
        assert strip_aidt("Aidt grant sam github") == "grant sam github"

    def test_bare_aidt_returns_empty(self):
        assert strip_aidt("aidt") == ""

    def test_no_aidt_unchanged(self):
        assert strip_aidt("kill") == "kill"

    def test_strips_with_extra_whitespace(self):
        assert strip_aidt("aidt  kill") == "kill"

    def test_empty_string(self):
        assert strip_aidt("") == ""


# ---------------------------------------------------------------------------
# parse_slack_slash
# ---------------------------------------------------------------------------


class TestParseSlackSlash:
    def test_kill_no_args_produces_kill_verb(self):
        cmd = parse_slack_slash("/kill", {"text": ""})
        assert cmd is not None
        assert cmd.verb == "kill"
        assert cmd.scope == SCOPE_AGENT
        assert cmd.transport == "slack"

    def test_kill_with_agent_name(self):
        cmd = parse_slack_slash("/kill", {"text": "sam"})
        assert cmd is not None
        assert cmd.verb == "kill"
        assert cmd.args == ["sam"]
        assert cmd.scope == SCOPE_AGENT

    def test_kill_all_produces_killall(self):
        cmd = parse_slack_slash("/kill", {"text": "all"})
        assert cmd is not None
        assert cmd.verb == "killall"
        assert cmd.scope == SCOPE_GLOBAL

    def test_kill_star_produces_killall(self):
        cmd = parse_slack_slash("/kill", {"text": "*"})
        assert cmd is not None
        assert cmd.verb == "killall"
        assert cmd.scope == SCOPE_GLOBAL

    def test_kill_everywhere_produces_killall(self):
        cmd = parse_slack_slash("/kill", {"text": "everywhere"})
        assert cmd is not None
        assert cmd.verb == "killall"
        assert cmd.scope == SCOPE_GLOBAL

    def test_kill_all_case_insensitive(self):
        cmd = parse_slack_slash("/kill", {"text": "ALL"})
        assert cmd is not None
        assert cmd.verb == "killall"

    def test_killall_slash_produces_killall(self):
        cmd = parse_slack_slash("/killall", {"text": ""})
        assert cmd is not None
        assert cmd.verb == "killall"
        assert cmd.scope == SCOPE_GLOBAL

    def test_tasks_list(self):
        cmd = parse_slack_slash("/tasks", {"text": "list"})
        assert cmd is not None
        assert cmd.verb == "tasks"
        assert cmd.scope == SCOPE_AGENT
        assert cmd.args == ["list"]

    def test_tasks_no_args(self):
        cmd = parse_slack_slash("/tasks", {"text": ""})
        assert cmd is not None
        assert cmd.verb == "tasks"
        assert cmd.args == []

    def test_tasks_pause_with_id(self):
        cmd = parse_slack_slash("/tasks", {"text": "pause abc-123"})
        assert cmd is not None
        assert cmd.verb == "tasks"
        assert cmd.args == ["pause", "abc-123"]

    def test_tasks_create(self):
        cmd = parse_slack_slash("/tasks", {"text": "create"})
        assert cmd is not None
        assert cmd.verb == "tasks"
        assert cmd.args == ["create"]

    def test_dev_prefix_stripped_kill(self):
        cmd = parse_slack_slash("/dev-kill", {"text": ""})
        assert cmd is not None
        assert cmd.verb == "kill"

    def test_dev_prefix_stripped_tasks(self):
        cmd = parse_slack_slash("/dev-tasks", {"text": "list"})
        assert cmd is not None
        assert cmd.verb == "tasks"

    def test_staging_prefix_stripped(self):
        cmd = parse_slack_slash("/staging-kill", {"text": "all"})
        assert cmd is not None
        assert cmd.verb == "killall"

    def test_conversation_ref_passed_through(self):
        cmd = parse_slack_slash("/kill", {"text": ""}, conversation_ref="slack:C1:1.0")
        assert cmd is not None
        assert cmd.conversation_ref == "slack:C1:1.0"

    def test_principal_ref_passed_through(self):
        cmd = parse_slack_slash("/kill", {"text": ""}, principal_ref="slack:U123")
        assert cmd is not None
        assert cmd.principal_ref == "slack:U123"

    def test_transport_is_slack(self):
        cmd = parse_slack_slash("/kill", {"text": ""})
        assert cmd is not None
        assert cmd.transport == "slack"

    def test_subject_ref_always_none(self):
        cmd = parse_slack_slash("/kill", {"text": "sam"})
        assert cmd is not None
        assert cmd.subject_ref is None

    def test_missing_text_key_handled(self):
        cmd = parse_slack_slash("/kill", {})
        assert cmd is not None
        assert cmd.verb == "kill"

    def test_none_text_value_handled(self):
        cmd = parse_slack_slash("/tasks", {"text": None})
        assert cmd is not None
        assert cmd.verb == "tasks"


# ---------------------------------------------------------------------------
# parse_from_message
# ---------------------------------------------------------------------------


class TestParseFromMessage:
    def test_mention_kill(self):
        cmd = parse_from_message("<@U123> kill")
        assert cmd is not None
        assert cmd.verb == "kill"
        assert cmd.scope == SCOPE_AGENT

    def test_mention_killall(self):
        cmd = parse_from_message("<@U123> killall")
        assert cmd is not None
        assert cmd.verb == "killall"
        assert cmd.scope == SCOPE_GLOBAL

    def test_aidt_kill(self):
        cmd = parse_from_message("aidt kill")
        assert cmd is not None
        assert cmd.verb == "kill"

    def test_aidt_case_insensitive(self):
        cmd = parse_from_message("AIDT kill")
        assert cmd is not None
        assert cmd.verb == "kill"

    def test_aidt_grant(self):
        cmd = parse_from_message("aidt grant sam github")
        assert cmd is not None
        assert cmd.verb == "grant"
        assert cmd.args == ["sam", "github"]

    def test_aidt_list_packs(self):
        cmd = parse_from_message("aidt list packs")
        assert cmd is not None
        assert cmd.verb == "list packs"
        assert cmd.scope == SCOPE_GLOBAL

    def test_aidt_who_has(self):
        cmd = parse_from_message("aidt who has github")
        assert cmd is not None
        assert cmd.verb == "who has"
        assert cmd.args == ["github"]

    def test_bare_aidt_returns_help(self):
        cmd = parse_from_message("aidt")
        assert cmd is not None
        assert cmd.verb == "help"

    def test_mention_bare_returns_help(self):
        cmd = parse_from_message("<@U123>")
        assert cmd is not None
        assert cmd.verb == "help"

    def test_non_command_text_returns_none(self):
        assert parse_from_message("hello, can you review this PR?") is None

    def test_mention_with_non_command_returns_none(self):
        assert parse_from_message("<@U123> please review my PR") is None

    def test_transport_is_slack(self):
        cmd = parse_from_message("aidt kill")
        assert cmd is not None
        assert cmd.transport == "slack"

    def test_context_fields_passed_through(self):
        cmd = parse_from_message(
            "aidt kill",
            conversation_ref="slack:C1:1.0",
            principal_ref="slack:U_op",
        )
        assert cmd is not None
        assert cmd.conversation_ref == "slack:C1:1.0"
        assert cmd.principal_ref == "slack:U_op"


# ---------------------------------------------------------------------------
# Smoke probes: end-to-end through shim → parser → handler
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_dispatch_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPATCH_WORKSPACE_ROOT", str(tmp_path))


@pytest.fixture
def respond():
    return AsyncMock()


@pytest.fixture
def ack():
    return AsyncMock()


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True})
    return client


class TestKillSmoke:
    """End-to-end smoke: shim → parser → handle_kill_command_from_parsed."""

    @pytest.mark.asyncio
    async def test_kill_agent_scoped_fires_kill(self, ack, respond, mock_client, tmp_path):
        """Agent-scoped kill resolves agent from resolver and fires the kill handler."""
        from router.commands.slack_shim import parse_slack_slash
        from router.kill_command import handle_kill_command_from_parsed
        from router.stuck_guard import GuardConfig, StuckGuard, make_task_id

        guard = StuckGuard(config=GuardConfig(mode="dry-run", post_mortem_dir=str(tmp_path)))

        body = {"channel_id": "C1", "message_ts": "1.0", "user_id": "U_op", "text": ""}

        with patch("router.kill_command.get_agent_map", return_value={"sam": {}}):
            cmd = parse_slack_slash(
                "/kill",
                body,
                conversation_ref="slack:C1:1.0",
                principal_ref="slack:U_op",
            )
            assert cmd is not None
            assert cmd.verb == "kill"
            assert cmd.scope == SCOPE_AGENT

            await handle_kill_command_from_parsed(
                cmd,
                ack=ack,
                body=body,
                respond=respond,
                client=mock_client,
                guard=guard,
                active_agent_resolver=lambda ch, ts: "sam",
            )

        ack.assert_called_once()
        respond.assert_called_once()
        # The handler should mark the task halted (even with no prior task,
        # the guard creates it). Verify the guard registered the task as halted.
        task_id = make_task_id("C1", "1.0", "sam")
        assert guard.is_halted(task_id)

    @pytest.mark.asyncio
    async def test_kill_no_agent_returns_scope_error(self, ack, respond, mock_client):
        """Agent-scoped kill with no resolvable agent → hard scope error."""
        from router.commands.slack_shim import parse_slack_slash
        from router.kill_command import handle_kill_command_from_parsed

        body = {"channel_id": "C1", "message_ts": "1.0", "user_id": "U_op", "text": ""}

        with patch("router.kill_command.get_agent_map", return_value={"sam": {}}):
            cmd = parse_slack_slash("/kill", body)
            assert cmd is not None
            assert cmd.verb == "kill"

            await handle_kill_command_from_parsed(
                cmd,
                ack=ack,
                body=body,
                respond=respond,
                client=mock_client,
                guard=None,
                active_agent_resolver=None,
            )

        ack.assert_called_once()
        respond.assert_called_once()
        text = respond.call_args.kwargs.get("text") or respond.call_args.args[0]
        assert "kill needs an agent" in text

    @pytest.mark.asyncio
    async def test_killall_global_fires_fleet_kill(self, ack, respond, mock_client, tmp_path):
        """Global killall through shim fires _handle_fleet_kill (no agent required)."""
        from router.commands.slack_shim import parse_slack_slash
        from router.kill_command import handle_kill_command_from_parsed

        body = {"channel_id": "C1", "message_ts": "1.0", "user_id": "U_op", "text": "all"}

        with patch("router.kill_command.get_agent_map", return_value={}):
            with patch("router.kill_command.get_default_guard") as mock_guard_fn:
                from router.stuck_guard import GuardConfig, StuckGuard

                guard = StuckGuard(config=GuardConfig(mode="dry-run", post_mortem_dir=str(tmp_path)))
                mock_guard_fn.return_value = guard

                cmd = parse_slack_slash(
                    "/kill",
                    body,
                    conversation_ref="slack:C1:1.0",
                    principal_ref="slack:U_op",
                )
                assert cmd is not None
                assert cmd.verb == "killall"
                assert cmd.scope == SCOPE_GLOBAL

                await handle_kill_command_from_parsed(
                    cmd,
                    ack=ack,
                    body=body,
                    respond=respond,
                    client=mock_client,
                )

        ack.assert_called_once()
        respond.assert_called_once()
        text = respond.call_args.kwargs.get("text") or respond.call_args.args[0]
        assert "No active tasks" in text or "kill" in text.lower()

    @pytest.mark.asyncio
    async def test_killall_slash_command_routes_global(self, ack, respond, mock_client, tmp_path):
        """/killall slash command routes to global fleet kill via shim."""
        from router.commands.slack_shim import parse_slack_slash
        from router.kill_command import handle_kill_command_from_parsed

        body = {"channel_id": "C1", "message_ts": "1.0", "user_id": "U_op", "text": ""}

        with patch("router.kill_command.get_agent_map", return_value={}):
            with patch("router.kill_command.get_default_guard") as mock_guard_fn:
                from router.stuck_guard import GuardConfig, StuckGuard

                guard = StuckGuard(config=GuardConfig(mode="dry-run", post_mortem_dir=str(tmp_path)))
                mock_guard_fn.return_value = guard

                cmd = parse_slack_slash("/killall", body)
                assert cmd is not None
                assert cmd.verb == "killall"
                assert cmd.scope == SCOPE_GLOBAL

                await handle_kill_command_from_parsed(
                    cmd,
                    ack=ack,
                    body=body,
                    respond=respond,
                    client=mock_client,
                )

        ack.assert_called_once()
        respond.assert_called_once()


class TestTasksSmoke:
    """Smoke: shim → grammar parser → handle_tasks_command_from_parsed."""

    @pytest.mark.asyncio
    async def test_tasks_list_routes_through_parser(self, ack, respond, tmp_path):
        """'/tasks list' slash body → grammar parser → handler dispatches to list."""
        from unittest.mock import MagicMock

        from router.commands.grammar import parse
        from router.scheduled_tasks.handlers import handle_tasks_command_from_parsed
        from router.scheduled_tasks.store import ScheduledTaskStore

        db_path = tmp_path / "tasks.db"
        store = ScheduledTaskStore(str(db_path))
        client = MagicMock()
        body = {"channel_id": "C1", "message_ts": "1.0", "user_id": "U_op", "text": "list"}

        # Shim constructs "tasks list" from the slash body and calls parse()
        body_text = (body.get("text") or "").strip()
        cmd = parse(f"tasks {body_text}".strip(), transport="slack")
        assert cmd is not None
        assert cmd.verb == "tasks"
        assert cmd.args == ["list"]

        await handle_tasks_command_from_parsed(
            cmd,
            ack=ack,
            body=body,
            client=client,
            respond=respond,
            store=store,
            agent_resolver=lambda b: "sam",
        )

        ack.assert_called_once()
        respond.assert_called_once()

    @pytest.mark.asyncio
    async def test_tasks_create_opens_modal_path(self, ack, respond, tmp_path):
        """'tasks create' still dispatches to the Slack modal-open path (not changed)."""
        from unittest.mock import AsyncMock, MagicMock

        from router.commands.grammar import parse
        from router.scheduled_tasks.handlers import handle_tasks_command_from_parsed
        from router.scheduled_tasks.store import ScheduledTaskStore

        db_path = tmp_path / "tasks.db"
        store = ScheduledTaskStore(str(db_path))
        client = MagicMock()
        client.views_open = AsyncMock(return_value={"ok": True})
        body = {
            "channel_id": "C1",
            "message_ts": "1.0",
            "user_id": "U_op",
            "text": "create",
            "trigger_id": "T123",
        }

        body_text = (body.get("text") or "").strip()
        cmd = parse(f"tasks {body_text}".strip(), transport="slack")
        assert cmd is not None
        assert cmd.verb == "tasks"
        assert cmd.args == ["create"]

        await handle_tasks_command_from_parsed(
            cmd,
            ack=ack,
            body=body,
            client=client,
            respond=respond,
            store=store,
            agent_resolver=lambda b: "sam",
        )

        ack.assert_called_once()
        client.views_open.assert_called_once()
