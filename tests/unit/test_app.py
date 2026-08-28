"""Unit tests for router.app — Slack event handling and session management.

Tests mock all external dependencies (Slack API, dispatcher, session manager)
so no Slack connection or Docker daemon is needed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import router.runtime as router_runtime

pytestmark = pytest.mark.unit


# We need to patch module-level side effects before importing app.
# app.py calls load_dotenv(), load_config(), and creates an AsyncApp at import time.


@pytest.fixture()
def app_module(monkeypatch, tmp_path):
    """Import router.app with all module-level side effects mocked."""
    monkeypatch.setenv("LISA_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("LISA_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("LISA_SIGNING_SECRET", "test-secret")
    monkeypatch.setenv("SAM_BOT_TOKEN", "xoxb-sam-test")
    monkeypatch.setenv("SAM_APP_TOKEN", "xapp-sam-test")
    monkeypatch.setenv("SAM_SIGNING_SECRET", "sam-test-secret")
    monkeypatch.setenv("ROUTER_INTERNAL_TOKEN", "test-internal-token")

    with (
        patch("router.app.AsyncApp") as mock_app_cls,
        patch("router.app.AsyncSocketModeHandler"),
        patch("router.app.load_dotenv"),
    ):
        mock_bolt_app = MagicMock()
        mock_bolt_app.client = MagicMock()
        mock_app_cls.return_value = mock_bolt_app

        import importlib  # noqa: E402

        import router.app  # noqa: E402
        import router.slack_events  # noqa: E402
        import router.threads.state as thread_state_mod  # noqa: E402

        importlib.reload(router.app)

        # The reload above rebuilds router.app, but the shared state now
        # lives in router.slack_events / router.runtime (not reloaded) —
        # reset it explicitly so tests stay isolated.
        router.slack_events._seen_events.clear()
        router_runtime.bot_user_id_by_agent.clear()
        router_runtime.bot_user_map.clear()
        router_runtime.dispatch_bot_user_ids.clear()
        router_runtime.workers_bot_user_id = None
        router_runtime.discord_adapters.clear()

        # Patch after reload so the module-level names are overridden
        monkeypatch.setattr(router.slack_events, "needs_curation", lambda *a, **kw: False)
        monkeypatch.setattr(router.slack_events, "curate_agent_memory", AsyncMock())
        monkeypatch.setattr(router.app, "start_internal_server", AsyncMock(return_value=MagicMock()))

        # Isolate the thread-state store: point the default store at a fresh
        # temp SQLite file so tests don't share state or pollute the CWD.
        thread_state_mod.reset_default_store()
        monkeypatch.setattr(
            thread_state_mod,
            "DEFAULT_DB_PATH",
            str(tmp_path / "thread_state.db"),
        )

        yield router.app

        thread_state_mod.reset_default_store()


@pytest.fixture()
def isolated_settings(tmp_path):
    """Point the settings singleton at tmp-backed stores; yields the SecretStore.

    Tests that need a secret "in the store" call ``set_str`` on the yielded
    store; tests that need it absent just take the fixture (fresh empty store).
    """
    from router import settings as settings_mod
    from router.packs.secret_store import SecretStore
    from router.settings import RuntimeSettings

    secret_store = SecretStore(path=tmp_path / "secrets.json")
    settings_mod.reset_settings_for_tests(
        RuntimeSettings(path=tmp_path / "runtime.json", ttl=0.0, secret_store=secret_store)
    )
    yield secret_store
    settings_mod.reset_settings_for_tests(None)


# ── _handle_event ───────────────────────────────────────────────────


class TestHandleEvent:
    """Tests for the main event handler."""

    @pytest.mark.asyncio
    async def test_ignores_bot_messages(self, app_module):
        """Events from bots should be ignored to prevent loops."""
        event = {"bot_id": "B001", "text": "bot message", "channel": "C001", "ts": "1.0"}
        say = AsyncMock()
        client = AsyncMock()
        await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
        say.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_bot_subtype(self, app_module):
        """Events with subtype bot_message should be ignored."""
        event = {"subtype": "bot_message", "text": "bot msg", "channel": "C001", "ts": "1.0"}
        say = AsyncMock()
        client = AsyncMock()
        await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
        say.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_agent_returns_early(self, app_module):
        """If receiving_agent is not in agent map, handler should return early."""
        event = {"text": "hello", "channel": "C001", "user": "U001", "ts": "1.0"}
        say = AsyncMock()
        client = AsyncMock()
        await app_module._handle_event(event, say, client, receiving_agent="nonexistent", was_mentioned=True)
        say.assert_not_called()

    @pytest.mark.asyncio
    async def test_exit_trigger_calls_handle_clean_exit(self, app_module):
        """Exit trigger phrase should invoke handle_clean_exit with the session thread_history."""
        history = [{"user": "U001", "text": "hello"}, {"user": "lisa", "text": "hi there"}]
        event = {
            "text": "thanks",
            "channel": "C001",
            "user": "U001",
            "ts": "1.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()

        with (
            patch(
                "router.slack_events.find_session_by_thread",
                return_value={"session_id": "s1", "agent_name": "lisa", "thread_history": history},
            ),
            patch("router.slack_events.update_activity"),
            patch("router.slack_events.handle_clean_exit", new_callable=AsyncMock, return_value=2) as mock_exit,
        ):
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
            mock_exit.assert_called_once()
            call_kwargs = mock_exit.call_args[1]
            assert call_kwargs["thread_history"] == history, "must pass session thread_history, not []"
            say.assert_called_once()
            assert "welcome" in say.call_args[1]["text"].lower() or "saved" in say.call_args[1]["text"].lower()

    @pytest.mark.asyncio
    async def test_exit_trigger_passes_session_thread_history(self, app_module):
        """handle_clean_exit must receive the populated session thread_history, not a hardcoded []."""
        history = [{"user": "U001", "text": "fix the bug"}, {"user": "lisa", "text": "done!"}]
        event = {
            "text": "thanks bye",
            "channel": "C001",
            "user": "U001",
            "ts": "2.0",
            "thread_ts": "2.0",
        }
        say = AsyncMock()
        client = AsyncMock()

        with (
            patch(
                "router.slack_events.find_session_by_thread",
                return_value={"session_id": "s2", "agent_name": "lisa", "thread_history": history},
            ),
            patch("router.slack_events.update_activity"),
            patch("router.slack_events.handle_clean_exit", new_callable=AsyncMock, return_value=1) as mock_exit,
        ):
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
            _, kwargs = mock_exit.call_args
            assert kwargs["thread_history"] is history

    @pytest.mark.asyncio
    async def test_exit_trigger_no_confirmation_when_empty_extraction(self, app_module):
        """No confirmation message is sent when memory extraction yields nothing (count=0)."""
        event = {
            "text": "thanks",
            "channel": "C001",
            "user": "U001",
            "ts": "3.0",
            "thread_ts": "3.0",
        }
        say = AsyncMock()
        client = AsyncMock()

        with (
            patch(
                "router.slack_events.find_session_by_thread",
                return_value={"session_id": "s3", "agent_name": "lisa", "thread_history": []},
            ),
            patch("router.slack_events.update_activity"),
            patch("router.slack_events.handle_clean_exit", new_callable=AsyncMock, return_value=0),
        ):
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
            say.assert_not_called()

    @pytest.mark.asyncio
    async def test_exit_trigger_calls_cleanup_session(self, app_module):
        """Clean exit should remove the session from the store via cleanup_session."""
        event = {
            "text": "thanks",
            "channel": "C001",
            "user": "U001",
            "ts": "1.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()

        with (
            patch(
                "router.slack_events.find_session_by_thread",
                return_value={"session_id": "s1", "agent_name": "lisa", "thread_history": []},
            ),
            patch("router.slack_events.update_activity"),
            patch("router.slack_events.handle_clean_exit", new_callable=AsyncMock, return_value=1),
            patch("router.slack_events.cleanup_session") as mock_cleanup,
        ):
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
            mock_cleanup.assert_called_once_with("s1")

    @pytest.mark.asyncio
    async def test_exit_trigger_handles_exception(self, app_module):
        """If handle_clean_exit raises, no confirmation is sent (nothing was persisted)."""
        event = {
            "text": "thanks",
            "channel": "C001",
            "user": "U001",
            "ts": "1.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()

        with (
            patch(
                "router.slack_events.find_session_by_thread",
                return_value={"session_id": "s1", "agent_name": "lisa", "thread_history": []},
            ),
            patch("router.slack_events.update_activity"),
            patch("router.slack_events.handle_clean_exit", new_callable=AsyncMock, side_effect=Exception("boom")),
            patch("router.slack_events.cleanup_session"),
        ):
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
            say.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_success_replies_in_thread(self, app_module):
        """Successful dispatch should reply with the agent's response in-thread."""
        event = {
            "text": "hello Lisa",
            "channel": "C001",
            "user": "U001",
            "ts": "1.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()
        client.reactions_add = AsyncMock()

        with (
            patch("router.slack_events.find_session_by_thread", return_value=None),
            patch("router.slack_events.create_session", return_value={"session_id": "s1"}),
            patch("router.slack_events.dispatch", new_callable=AsyncMock, return_value={"response": "Hi there!"}),
            patch("router.slack_events.update_activity"),
            patch("router.slack_events.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
            say.assert_called_once()
            assert say.call_args[1]["text"] == "Hi there!"

    @pytest.mark.asyncio
    async def test_dispatch_error_sends_categorised_message(self, app_module):
        """If dispatch raises a generic Exception, handler posts an internal-error message with a corr id."""
        event = {
            "text": "hello Lisa",
            "channel": "C001",
            "user": "U001",
            "ts": "1.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()
        client.reactions_add = AsyncMock()

        with (
            patch("router.slack_events.get_agent_map", return_value={"lisa": {"container": "lisa", "name": "Lisa"}}),
            patch("router.slack_events.find_session_by_thread", return_value=None),
            patch("router.slack_events.create_session", return_value={"session_id": "s1"}),
            patch("router.slack_events.dispatch", new_callable=AsyncMock, side_effect=Exception("dispatch failed")),
            patch("router.slack_events.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
            say.assert_called_once()
            text = say.call_args[1]["text"]
            assert "internal error" in text.lower()
            assert "bram" in text.lower()

    @pytest.mark.asyncio
    async def test_dispatch_529_sends_overload_message(self, app_module):
        """A 529 ApiError should post the overload user message."""
        from router.dispatcher import ApiError

        event = {
            "text": "hello Lisa",
            "channel": "C001",
            "user": "U001",
            "ts": "1.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()
        client.reactions_add = AsyncMock()

        with (
            patch("router.slack_events.get_agent_map", return_value={"lisa": {"container": "lisa", "name": "Lisa"}}),
            patch("router.slack_events.find_session_by_thread", return_value=None),
            patch("router.slack_events.create_session", return_value={"session_id": "s1"}),
            patch(
                "router.slack_events.dispatch",
                new_callable=AsyncMock,
                side_effect=ApiError(529, "overloaded"),
            ),
            patch("router.slack_events.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
            say.assert_called_once()
            text = say.call_args[1]["text"]
            assert "overloaded" in text.lower()
            assert "retry" in text.lower()

    @pytest.mark.asyncio
    async def test_dispatch_timeout_sends_timeout_message(self, app_module):
        """A DispatchTimeoutError should post the timeout user message."""
        from router.dispatcher import DispatchTimeoutError

        event = {
            "text": "hello Lisa",
            "channel": "C001",
            "user": "U001",
            "ts": "1.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()
        client.reactions_add = AsyncMock()

        with (
            patch("router.slack_events.get_agent_map", return_value={"lisa": {"container": "lisa", "name": "Lisa"}}),
            patch("router.slack_events.find_session_by_thread", return_value=None),
            patch("router.slack_events.create_session", return_value={"session_id": "s1"}),
            patch(
                "router.slack_events.dispatch",
                new_callable=AsyncMock,
                side_effect=DispatchTimeoutError("timed out after 30s"),
            ),
            patch("router.slack_events.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
            say.assert_called_once()
            text = say.call_args[1]["text"]
            assert "time" in text.lower()

    @pytest.mark.asyncio
    async def test_dispatch_cli_exit1_sends_cli_error_message(self, app_module):
        """A generic DispatchError (CLI exit 1) should post the worker-error message."""
        from router.dispatcher import DispatchError

        event = {
            "text": "hello Lisa",
            "channel": "C001",
            "user": "U001",
            "ts": "1.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()
        client.reactions_add = AsyncMock()

        with (
            patch("router.slack_events.get_agent_map", return_value={"lisa": {"container": "lisa", "name": "Lisa"}}),
            patch("router.slack_events.find_session_by_thread", return_value=None),
            patch("router.slack_events.create_session", return_value={"session_id": "s1"}),
            patch(
                "router.slack_events.dispatch",
                new_callable=AsyncMock,
                side_effect=DispatchError("agent lisa CLI exited with code 1"),
            ),
            patch("router.slack_events.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
            say.assert_called_once()
            text = say.call_args[1]["text"]
            assert "worker" in text.lower() or "error" in text.lower()

    @pytest.mark.asyncio
    async def test_dispatch_error_message_includes_corr_id(self, app_module):
        """Every error message must embed a correlation id the user can quote."""
        import re

        event = {
            "text": "hello Lisa",
            "channel": "C001",
            "user": "U001",
            "ts": "1.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()
        client.reactions_add = AsyncMock()

        with (
            patch("router.slack_events.get_agent_map", return_value={"lisa": {"container": "lisa", "name": "Lisa"}}),
            patch("router.slack_events.find_session_by_thread", return_value=None),
            patch("router.slack_events.create_session", return_value={"session_id": "s1"}),
            patch("router.slack_events.dispatch", new_callable=AsyncMock, side_effect=Exception("boom")),
            patch("router.slack_events.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
            text = say.call_args[1]["text"]
            # 8-char hex corr_id should appear somewhere in the message
            assert re.search(r"[0-9a-f]{8}", text), f"No corr_id found in: {text!r}"

    @pytest.mark.asyncio
    async def test_existing_session_updates_activity(self, app_module):
        """When reusing an existing session, update_activity should be called."""
        event = {
            "text": "hello Lisa",
            "channel": "C001",
            "user": "U001",
            "ts": "1.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()
        client.reactions_add = AsyncMock()

        with (
            patch(
                "router.slack_events.find_session_by_thread",
                return_value={"session_id": "s1", "agent_name": "lisa"},
            ),
            patch("router.slack_events.update_activity") as mock_update,
            patch("router.slack_events.dispatch", new_callable=AsyncMock, return_value={"response": "Hi!"}),
            patch("router.slack_events.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
            mock_update.assert_called()

    @pytest.mark.asyncio
    async def test_reaction_failure_is_non_critical(self, app_module):
        """If reactions_add fails, dispatch should still proceed."""
        event = {
            "text": "hello Lisa",
            "channel": "C001",
            "user": "U001",
            "ts": "1.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()
        client.reactions_add = AsyncMock(side_effect=Exception("rate limited"))

        with (
            patch("router.slack_events.find_session_by_thread", return_value=None),
            patch("router.slack_events.create_session", return_value={"session_id": "s1"}),
            patch("router.slack_events.dispatch", new_callable=AsyncMock, return_value={"response": "Hi!"}),
            patch("router.slack_events.update_activity"),
            patch("router.slack_events.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
            say.assert_called_once()
            assert say.call_args[1]["text"] == "Hi!"

    @pytest.mark.asyncio
    async def test_human_app_mention_in_active_thread_dispatches_once(self, app_module):
        """Human @-mention in an active thread must dispatch exactly once (issue #278).

        Slack delivers both ``app_mention`` and ``message`` for the same human
        channel @-mention.  The first arrival (``app_mention``) calls
        ``_handle_event(was_mentioned=True)``; the second (``message`` falling
        through the active-thread branch) calls ``_handle_event(was_mentioned=False)``.
        The dedup guard keyed on ``client_msg_id`` must short-circuit the second
        invocation so only one ``dispatch`` call is made.
        """
        event = {
            "client_msg_id": "cmid-278-dedup-test",
            "text": "<@U_BOT_LISA> can you check this?",
            "channel": "C001",
            "user": "U_HUMAN",
            "ts": "2.0",
            "thread_ts": "1.0",
            "channel_type": "channel",
        }
        say = AsyncMock()
        client = AsyncMock()
        client.reactions_add = AsyncMock()

        # Ensure a clean dedup store for this test.
        app_module._seen_events.clear()
        try:
            with (
                patch(
                    "router.slack_events.get_agent_map", return_value={"lisa": {"container": "lisa", "name": "Lisa"}}
                ),
                patch("router.slack_events.find_session_by_thread", return_value=None),
                patch("router.slack_events.create_session", return_value={"session_id": "s1"}),
                patch(
                    "router.slack_events.dispatch",
                    new_callable=AsyncMock,
                    return_value={"response": "On it!"},
                ) as mock_dispatch,
                patch("router.slack_events.add_to_thread_history"),
            ):
                # Simulate app_mention arriving first.
                await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
                # Simulate message arriving second via active-thread fall-through.
                await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=False)

            mock_dispatch.assert_awaited_once()
        finally:
            app_module._seen_events.clear()

    @pytest.mark.asyncio
    async def test_app_mention_and_message_dedup_without_shared_client_msg_id(self, app_module, isolated_settings):
        """One human @-mention dispatches once even though ``app_mention`` carries no
        ``client_msg_id`` (issue #386).

        Slack only populates ``client_msg_id`` on the ``message`` delivery, never on
        ``app_mention``. Keying dedup on ``client_msg_id`` therefore let the two
        deliveries of one mention carry different identities and both reach dispatch.
        The two event dicts below model that exactly: the ``app_mention`` delivery has
        no ``client_msg_id`` at all, the ``message`` delivery has one — but both share
        ``(channel, ts)``, which the dedup key must key on instead.
        """
        app_mention_event = {
            "text": "<@U_BOT_LISA> can you check this?",
            "channel": "C020",
            "user": "U_HUMAN",
            "ts": "20.0",
            "thread_ts": "19.0",
            "channel_type": "channel",
        }
        message_event = {
            **app_mention_event,
            "client_msg_id": "cmid-386-message-only",
        }
        say = AsyncMock()
        client = AsyncMock()
        client.reactions_add = AsyncMock()

        app_module._seen_events.clear()
        try:
            with (
                patch(
                    "router.slack_events.get_agent_map", return_value={"lisa": {"container": "lisa", "name": "Lisa"}}
                ),
                patch("router.slack_events.find_session_by_thread", return_value=None),
                patch("router.slack_events.create_session", return_value={"session_id": "s20"}),
                patch(
                    "router.slack_events.dispatch",
                    new_callable=AsyncMock,
                    return_value={"response": "On it!"},
                ) as mock_dispatch,
                patch("router.slack_events.add_to_thread_history"),
            ):
                # app_mention arrives first — no client_msg_id, as Slack actually sends it.
                await app_module._handle_event(
                    app_mention_event, say, client, receiving_agent="lisa", was_mentioned=True
                )
                # message arrives second — carries client_msg_id, same (channel, ts).
                await app_module._handle_event(message_event, say, client, receiving_agent="lisa", was_mentioned=False)

            mock_dispatch.assert_awaited_once()
        finally:
            app_module._seen_events.clear()

    @pytest.mark.asyncio
    async def test_dedup_fallback_key_without_client_msg_id(self, app_module):
        """Dedup keys on ``(channel, ts)`` regardless of ``client_msg_id`` presence."""
        event = {
            # No client_msg_id — simulates a bot/system event that passed the bot guard.
            "text": "duplicate event",
            "channel": "C002",
            "user": "U002",
            "ts": "5.0",
            "thread_ts": "5.0",
        }
        say = AsyncMock()
        client = AsyncMock()
        client.reactions_add = AsyncMock()

        app_module._seen_events.clear()
        try:
            with (
                patch(
                    "router.slack_events.get_agent_map", return_value={"lisa": {"container": "lisa", "name": "Lisa"}}
                ),
                patch("router.slack_events.find_session_by_thread", return_value=None),
                patch("router.slack_events.create_session", return_value={"session_id": "s2"}),
                patch(
                    "router.slack_events.dispatch",
                    new_callable=AsyncMock,
                    return_value={"response": "Once!"},
                ) as mock_dispatch,
                patch("router.slack_events.add_to_thread_history"),
            ):
                await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
                await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)

            mock_dispatch.assert_awaited_once()
        finally:
            app_module._seen_events.clear()

    @pytest.mark.asyncio
    async def test_multi_agent_mention_both_agents_dispatch(self, app_module):
        """Two agents mentioned in the same message must both reach dispatch (issue #517).

        When @sam and @lisa are both mentioned, Slack delivers the event to each
        bot with the same ``client_msg_id``.  The dedup key is now agent-scoped
        so the second agent's event is NOT dropped.
        """
        event = {
            "client_msg_id": "cmid-517-multi-agent",
            "text": "<@U_SAM> <@U_LISA> do X",
            "channel": "C010",
            "user": "U_HUMAN",
            "ts": "10.0",
            "thread_ts": "10.0",
            "channel_type": "channel",
        }
        say = AsyncMock()
        client = AsyncMock()
        client.reactions_add = AsyncMock()

        app_module._seen_events.clear()
        try:
            with (
                patch(
                    "router.slack_events.get_agent_map",
                    return_value={
                        "sam": {"container": "sam", "name": "Sam"},
                        "lisa": {"container": "lisa", "name": "Lisa"},
                    },
                ),
                patch("router.slack_events.find_session_by_thread", return_value=None),
                patch("router.slack_events.create_session", return_value={"session_id": "s10"}),
                patch(
                    "router.slack_events.dispatch",
                    new_callable=AsyncMock,
                    return_value={"response": "On it!"},
                ) as mock_dispatch,
                patch("router.slack_events.add_to_thread_history"),
            ):
                await app_module._handle_event(event, say, client, receiving_agent="sam", was_mentioned=True)
                await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)

            assert mock_dispatch.await_count == 2, "Both agents must dispatch — neither should be dropped"
        finally:
            app_module._seen_events.clear()

    @pytest.mark.asyncio
    async def test_same_agent_double_delivery_still_deduped(self, app_module):
        """Same-agent double-delivery (app_mention + message) is still deduped (issue #517).

        The agent-scoped key must still catch ``app_mention`` + ``message`` arriving
        for the *same* agent with the same ``client_msg_id``.
        """
        event = {
            "client_msg_id": "cmid-517-same-agent-dedup",
            "text": "<@U_SAM> do Y",
            "channel": "C011",
            "user": "U_HUMAN",
            "ts": "11.0",
            "thread_ts": "11.0",
            "channel_type": "channel",
        }
        say = AsyncMock()
        client = AsyncMock()
        client.reactions_add = AsyncMock()

        app_module._seen_events.clear()
        try:
            with (
                patch("router.slack_events.get_agent_map", return_value={"sam": {"container": "sam", "name": "Sam"}}),
                patch("router.slack_events.find_session_by_thread", return_value=None),
                patch("router.slack_events.create_session", return_value={"session_id": "s11"}),
                patch(
                    "router.slack_events.dispatch",
                    new_callable=AsyncMock,
                    return_value={"response": "On it!"},
                ) as mock_dispatch,
                patch("router.slack_events.add_to_thread_history"),
            ):
                # First delivery (app_mention) → should dispatch.
                await app_module._handle_event(event, say, client, receiving_agent="sam", was_mentioned=True)
                # Second delivery (message fallthrough) → must be dropped.
                await app_module._handle_event(event, say, client, receiving_agent="sam", was_mentioned=False)

            mock_dispatch.assert_awaited_once()
        finally:
            app_module._seen_events.clear()


# ── handle_message ──────────────────────────────────────────────────


class TestHandleMessage:
    """Tests for the message event handler."""

    @pytest.mark.asyncio
    async def test_dm_is_handled(self, app_module):
        """Direct messages should always be handled."""
        event = {
            "channel_type": "im",
            "text": "hello",
            "channel": "D001",
            "user": "U001",
            "ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()
        client.reactions_add = AsyncMock()

        with (
            patch("router.slack_events.find_session_by_thread", return_value=None),
            patch("router.slack_events.create_session", return_value={"session_id": "s1"}),
            patch("router.slack_events.dispatch", new_callable=AsyncMock, return_value={"response": "Hi!"}),
            patch("router.slack_events.update_activity"),
            patch("router.slack_events.add_to_thread_history"),
        ):
            await app_module.handle_message(event, say, client, receiving_agent="lisa")
            say.assert_called_once()

    @pytest.mark.asyncio
    async def test_channel_thread_reply_when_active(self, app_module):
        """Thread reply in a channel routes to this agent when it is the active agent."""
        from router.threads.state import get_default_store

        get_default_store().set_active_agent("C001", "1.0", "lisa", mentioned=True)

        event = {
            "channel_type": "channel",
            "text": "follow up",
            "channel": "C001",
            "user": "U001",
            "ts": "2.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()
        client.reactions_add = AsyncMock()

        with (
            patch(
                "router.slack_events.find_session_by_thread",
                return_value={"session_id": "s1", "agent_name": "lisa"},
            ),
            patch("router.slack_events.update_activity"),
            patch("router.slack_events.dispatch", new_callable=AsyncMock, return_value={"response": "Reply!"}),
            patch("router.slack_events.add_to_thread_history"),
        ):
            await app_module.handle_message(event, say, client, receiving_agent="lisa")
            say.assert_called_once()

    @pytest.mark.asyncio
    async def test_channel_thread_reply_other_active_skipped(self, app_module):
        """When another agent is active in the thread, this agent skips the reply."""
        from router.threads.state import get_default_store

        get_default_store().set_active_agent("C001", "1.0", "dave", mentioned=True)

        event = {
            "channel_type": "channel",
            "text": "follow up",
            "channel": "C001",
            "user": "U001",
            "ts": "2.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()

        await app_module.handle_message(event, say, client, receiving_agent="lisa")
        say.assert_not_called()

    @pytest.mark.asyncio
    async def test_channel_message_with_other_bot_mention_skipped(self, app_module):
        """Channel message mentioning another bot defers to that bot's app_mention."""
        app_module._bot_user_id_by_agent["dave"] = "U_BOT_DAVE"
        try:
            event = {
                "channel_type": "channel",
                "text": "hey <@U_BOT_DAVE> please look",
                "channel": "C001",
                "user": "U001",
                "ts": "2.0",
                "thread_ts": "1.0",
            }
            say = AsyncMock()
            client = AsyncMock()
            await app_module.handle_message(event, say, client, receiving_agent="lisa")
            say.assert_not_called()
        finally:
            app_module._bot_user_id_by_agent.clear()

    @pytest.mark.asyncio
    async def test_channel_message_with_self_mention_handled(self, app_module):
        """Self-mention from a known dispatch bot is handled as ``was_mentioned=True``.

        Slack suppresses ``app_mention`` when a bot @-mentions itself, so if
        ``handle_message`` deferred on *any* known-bot mention the event would
        be silently dropped. This is the path used by the auto-review handoff
        where a dispatch worker pings its owning agent on completion.
        """
        app_module._bot_user_id_by_agent["sam"] = "U_BOT_SAM"
        app_module._dispatch_bot_user_ids.add("U_BOT_WORKER")
        try:
            event = {
                "channel_type": "channel",
                "text": "<@U_BOT_SAM> auto-review please",
                "channel": "C001",
                "user": "U_BOT_WORKER",
                "ts": "2.0",
                "thread_ts": "1.0",
            }
            say = AsyncMock()
            client = AsyncMock()
            with patch("router.slack_events._handle_event", new_callable=AsyncMock) as mock_handle:
                await app_module.handle_message(event, say, client, receiving_agent="sam")
                mock_handle.assert_awaited_once()
                kwargs = mock_handle.await_args.kwargs
                assert kwargs["receiving_agent"] == "sam"
                assert kwargs["was_mentioned"] is True
        finally:
            app_module._bot_user_id_by_agent.clear()
            app_module._dispatch_bot_user_ids.discard("U_BOT_WORKER")

    @pytest.mark.asyncio
    async def test_channel_message_human_self_mention_defers_to_app_mention(self, app_module):
        """A *human* @-mentioning this bot in a channel must NOT dispatch here.

        Slack fires both ``app_mention`` and ``message`` for the same event
        when a human mentions a bot. The ``app_mention`` handler already
        dispatches; if ``handle_message`` also dispatches on the self-mention
        branch we get two responses (issue #262). The branch must be gated on
        sender-is-a-known-dispatch-bot — preserving the auto-review path
        (test above) while ignoring the duplicate human-mention event.
        """
        app_module._bot_user_id_by_agent["sam"] = "U_BOT_SAM"
        # Note: human sender U_HUMAN is NOT in _dispatch_bot_user_ids.
        try:
            event = {
                "channel_type": "channel",
                "text": "<@U_BOT_SAM> review pr 258",
                "channel": "C001",
                "user": "U_HUMAN",
                "ts": "2.0",
                "thread_ts": "1.0",
            }
            say = AsyncMock()
            client = AsyncMock()
            with patch("router.slack_events._handle_event", new_callable=AsyncMock) as mock_handle:
                await app_module.handle_message(event, say, client, receiving_agent="sam")
                mock_handle.assert_not_awaited()
        finally:
            app_module._bot_user_id_by_agent.clear()

    @pytest.mark.asyncio
    async def test_workers_bot_self_mention_respects_handoff_flag(self, app_module, monkeypatch):
        """The self-mention branch shares ``_is_dispatch_bot_sender`` with the bot-message
        guard, so it cannot drift from it (issue #386).

        Before the fix, ``handle_message``'s self-mention branch used a raw
        ``sender in _dispatch_bot_user_ids`` membership check that ignored the
        workers-bot ``WORKER_MENTION_HANDOFF`` gate applied by
        ``_is_dispatch_bot_sender`` in the bot-message guard. With
        ``WORKER_MENTION_HANDOFF`` unset (default off), a workers-bot post that
        @-mentions the receiving agent's own bot must be dropped here too, not just
        downstream in ``_handle_event``.
        """
        router_runtime.workers_bot_user_id = "U_BOT_WORKERS"
        app_module._bot_user_id_by_agent["sam"] = "U_BOT_SAM"
        app_module._dispatch_bot_user_ids.add("U_BOT_WORKERS")
        monkeypatch.delenv("WORKER_MENTION_HANDOFF", raising=False)
        try:
            event = {
                "channel_type": "channel",
                "text": "<@U_BOT_SAM> auto-review please",
                "channel": "C001",
                "user": "U_BOT_WORKERS",
                "ts": "2.0",
                "thread_ts": "1.0",
            }
            say = AsyncMock()
            client = AsyncMock()
            with patch("router.slack_events._handle_event", new_callable=AsyncMock) as mock_handle:
                await app_module.handle_message(event, say, client, receiving_agent="sam")
                mock_handle.assert_not_awaited()
        finally:
            router_runtime.workers_bot_user_id = None
            app_module._bot_user_id_by_agent.clear()
            app_module._dispatch_bot_user_ids.discard("U_BOT_WORKERS")

    @pytest.mark.asyncio
    async def test_channel_message_with_self_and_other_bot_mention_defers(self, app_module):
        """When a different bot is also mentioned, defer — that bot's
        ``app_mention`` will fire and handle the event for both."""
        app_module._bot_user_id_by_agent.update({"sam": "U_BOT_SAM", "lisa": "U_BOT_LISA"})
        try:
            event = {
                "channel_type": "channel",
                "text": "<@U_BOT_SAM> <@U_BOT_LISA> heads up",
                "channel": "C001",
                "user": "U001",
                "ts": "2.0",
                "thread_ts": "1.0",
            }
            say = AsyncMock()
            client = AsyncMock()
            with patch("router.slack_events._handle_event", new_callable=AsyncMock) as mock_handle:
                await app_module.handle_message(event, say, client, receiving_agent="sam")
                mock_handle.assert_not_awaited()
        finally:
            app_module._bot_user_id_by_agent.clear()

    @pytest.mark.asyncio
    async def test_channel_message_no_thread_ignored(self, app_module):
        """Non-threaded channel message (not DM, no thread_ts) should be ignored."""
        event = {
            "channel_type": "channel",
            "text": "standalone message",
            "channel": "C001",
            "user": "U001",
            "ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()

        await app_module.handle_message(event, say, client, receiving_agent="lisa")
        say.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_error_logs_routing_dropped(self, app_module, caplog):
        """When get_active_agent raises, routing.dropped reason=store_error is logged."""
        import logging

        event = {
            "channel_type": "channel",
            "text": "follow up",
            "channel": "C001",
            "user": "U001",
            "ts": "2.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()

        with (
            patch(
                "router.slack_events.get_default_store",
                side_effect=Exception("db unavailable"),
            ),
            caplog.at_level(logging.WARNING, logger="router.app"),
        ):
            await app_module.handle_message(event, say, client, receiving_agent="lisa")

        say.assert_not_called()
        assert any("routing.dropped" in r.message and "store_error" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_no_active_agent_logs_routing_dropped(self, app_module, caplog):
        """When get_active_agent returns None and dispatch ownership is absent,
        routing.dropped reason=no_active_agent is logged."""
        import logging

        event = {
            "channel_type": "channel",
            "text": "follow up",
            "channel": "C001",
            "user": "U001",
            "ts": "2.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()

        mock_store = MagicMock()
        mock_store.get_active_agent.return_value = None

        with (
            patch("router.slack_events.get_default_store", return_value=mock_store),
            patch("router.slack_events._agent_owns_dispatch_thread", return_value=False),
            caplog.at_level(logging.WARNING, logger="router.app"),
        ):
            await app_module.handle_message(event, say, client, receiving_agent="lisa")

        say.assert_not_called()
        assert any("routing.dropped" in r.message and "no_active_agent" in r.message for r in caplog.records)


# ── agent handoff ───────────────────────────────────────────────────


class TestAgentHandoff:
    """Tests for mention-driven multi-agent handoffs."""

    @pytest.mark.asyncio
    async def test_app_mention_records_active_agent(self, app_module):
        """An app_mention establishes the receiving agent as the thread's active agent."""
        from router.threads.state import get_default_store

        event = {
            "text": "<@U_BOT_SAM> can you weigh in?",
            "channel": "C001",
            "user": "U001",
            "ts": "2.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()

        with (
            patch(
                "router.slack_events.get_agent_map",
                return_value={
                    "lisa": {"container": "lisa", "name": "Lisa"},
                    "sam": {"container": "sam", "name": "Sam"},
                },
            ),
            patch("router.slack_events.find_session_by_thread", return_value=None),
            patch(
                "router.slack_events.create_session",
                return_value={"session_id": "s1", "agent_name": "sam"},
            ),
            patch(
                "router.slack_events.dispatch",
                new_callable=AsyncMock,
                return_value={"response": "Sam here, I can help."},
            ) as mock_dispatch,
            patch("router.slack_events.update_activity"),
            patch("router.slack_events.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client, receiving_agent="sam", was_mentioned=True)
            mock_dispatch.assert_called_once()
            assert mock_dispatch.call_args.kwargs["agent_name"] == "sam"

        # Thread state was updated to sam.
        assert get_default_store().get_active_agent("C001", "1.0") == "sam"

    @pytest.mark.asyncio
    async def test_unmentioned_reply_routes_via_active_agent(self, app_module):
        """An un-mentioned reply in a thread routes via handle_message to the
        agent whose app is the thread's active agent."""
        from router.threads.state import get_default_store

        get_default_store().set_active_agent("C001", "1.0", "sam", mentioned=True)

        event = {
            "channel_type": "channel",
            "text": "ok what next?",
            "channel": "C001",
            "user": "U001",
            "ts": "3.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()

        with (
            patch(
                "router.slack_events.get_agent_map",
                return_value={
                    "lisa": {"container": "lisa", "name": "Lisa"},
                    "sam": {"container": "sam", "name": "Sam"},
                },
            ),
            patch("router.slack_events.find_session_by_thread", return_value=None),
            patch(
                "router.slack_events.create_session",
                return_value={"session_id": "s1", "agent_name": "sam"},
            ),
            patch(
                "router.slack_events.dispatch",
                new_callable=AsyncMock,
                return_value={"response": "Got it."},
            ) as mock_dispatch,
            patch("router.slack_events.update_activity"),
            patch("router.slack_events.add_to_thread_history"),
        ):
            # Sam's app handles the reply (active agent matches).
            await app_module.handle_message(event, say, client, receiving_agent="sam")
            mock_dispatch.assert_called_once()
            assert mock_dispatch.call_args.kwargs["agent_name"] == "sam"

            # Lisa's app skips (she is not the active agent).
            mock_dispatch.reset_mock()
            await app_module.handle_message(event, say, client, receiving_agent="lisa")
            mock_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_response_mentioning_other_agent_triggers_handoff(self, app_module):
        """If the agent's response @mentions another agent, the next message
        should route to that agent."""
        from router.threads.state import get_default_store

        event = {
            "text": "please decide",
            "channel": "C001",
            "user": "U001",
            "ts": "1.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()

        with (
            patch(
                "router.slack_events.get_agent_map",
                return_value={
                    "lisa": {"container": "lisa", "name": "Lisa"},
                    "dave": {"container": "dave", "name": "Dave"},
                },
            ),
            patch("router.slack_events.find_session_by_thread", return_value=None),
            patch(
                "router.slack_events.create_session",
                return_value={"session_id": "s1", "agent_name": "lisa"},
            ),
            patch(
                "router.slack_events.dispatch",
                new_callable=AsyncMock,
                return_value={"response": "I'll loop in @dave on this."},
            ),
            patch("router.slack_events.update_activity"),
            patch("router.slack_events.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)

        assert get_default_store().get_active_agent("C001", "1.0") == "dave"

    @pytest.mark.asyncio
    async def test_agent_self_mention_does_not_handoff(self, app_module):
        """An agent mentioning itself should not cause a handoff."""
        from router.threads.state import get_default_store

        event = {
            "text": "@lisa hi",
            "channel": "C001",
            "user": "U001",
            "ts": "1.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()

        with (
            patch(
                "router.slack_events.get_agent_map",
                return_value={"lisa": {"container": "lisa", "name": "Lisa"}},
            ),
            patch("router.slack_events.find_session_by_thread", return_value=None),
            patch(
                "router.slack_events.create_session",
                return_value={"session_id": "s1", "agent_name": "lisa"},
            ),
            patch(
                "router.slack_events.dispatch",
                new_callable=AsyncMock,
                return_value={"response": "Hi from @lisa again!"},
            ),
            patch("router.slack_events.update_activity"),
            patch("router.slack_events.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)

        assert get_default_store().get_active_agent("C001", "1.0") == "lisa"

    @pytest.mark.asyncio
    async def test_mentions_pass_bot_user_map_to_dispatcher(self, app_module):
        """Dispatcher should receive the bot_user_map so it can build a
        multi-agent transcript."""
        app_module._bot_user_map["U_BOT_LISA"] = "lisa"
        try:
            event = {
                "text": "hi",
                "channel": "C001",
                "user": "U001",
                "ts": "1.0",
                "thread_ts": "1.0",
            }
            say = AsyncMock()
            client = AsyncMock()

            with (
                patch("router.slack_events.find_session_by_thread", return_value=None),
                patch(
                    "router.slack_events.create_session",
                    return_value={"session_id": "s1", "agent_name": "lisa"},
                ),
                patch(
                    "router.slack_events.dispatch",
                    new_callable=AsyncMock,
                    return_value={"response": "ok"},
                ) as mock_dispatch,
                patch("router.slack_events.update_activity"),
                patch("router.slack_events.add_to_thread_history"),
            ):
                await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=False)
                kwargs = mock_dispatch.call_args.kwargs
                assert kwargs["bot_user_map"] == {"U_BOT_LISA": "lisa"}
        finally:
            app_module._bot_user_map.clear()

    @pytest.mark.asyncio
    async def test_dispatch_does_not_receive_max_token_budget(self, app_module):
        """Regression for issue #144.

        The context token budget is owned by ``router.dispatcher``: it reads
        ``MAX_CONTEXT_TOKENS`` (with a sane default) and is the single source
        of truth. ``app.py`` must not pass ``max_token_budget`` into
        ``dispatch(...)`` — that was the path by which ``config.py``'s stale
        4000 default silently overrode the dispatcher's 32000 in production.
        """
        event = {
            "text": "hi",
            "channel": "C001",
            "user": "U001",
            "ts": "1.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()

        with (
            patch("router.slack_events.find_session_by_thread", return_value=None),
            patch(
                "router.slack_events.create_session",
                return_value={"session_id": "s1", "agent_name": "lisa"},
            ),
            patch(
                "router.slack_events.dispatch",
                new_callable=AsyncMock,
                return_value={"response": "ok"},
            ) as mock_dispatch,
            patch("router.slack_events.update_activity"),
            patch("router.slack_events.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
            kwargs = mock_dispatch.call_args.kwargs
            assert "max_token_budget" not in kwargs, (
                "app.py must let the dispatcher resolve the token budget "
                "from MAX_CONTEXT_TOKENS; passing it from config.py reintroduces #144."
            )


# ── _make_event_handlers ────────────────────────────────────────────


class TestMakeEventHandlers:
    """Per-agent handler factories produce Bolt-compatible signatures."""

    def test_handlers_only_expose_bolt_recognized_params(self, app_module):
        """Bolt rejects unknown parameter names and leaves them unbound. The
        per-agent agent name must be captured via lexical closure, not via a
        default-arg parameter, so the visible signature stays ``(event, say,
        client)``."""
        import inspect

        on_app_mention, on_message = app_module._make_event_handlers("lisa")
        for handler in (on_app_mention, on_message):
            params = list(inspect.signature(handler).parameters)
            assert params == ["event", "say", "client"], (
                f"{handler.__name__} signature {params} contains parameters Bolt "
                "won't inject — agent_name should be captured via closure."
            )

    @pytest.mark.asyncio
    async def test_app_mention_handler_routes_to_receiving_agent(self, app_module):
        """The factory-built app_mention handler dispatches as the bound agent."""
        on_app_mention, _ = app_module._make_event_handlers("sam")
        say = AsyncMock()
        client = AsyncMock()
        with patch("router.slack_events._handle_event", new_callable=AsyncMock) as mock_handler:
            await on_app_mention({"text": "hi"}, say, client)
            mock_handler.assert_called_once_with({"text": "hi"}, say, client, receiving_agent="sam", was_mentioned=True)

    @pytest.mark.asyncio
    async def test_message_handler_routes_to_receiving_agent(self, app_module):
        """The factory-built message handler dispatches as the bound agent."""
        _, on_message = app_module._make_event_handlers("dave")
        say = AsyncMock()
        client = AsyncMock()
        with patch("router.slack_events.handle_message", new_callable=AsyncMock) as mock_handler:
            await on_message({"text": "hi"}, say, client)
            mock_handler.assert_called_once_with({"text": "hi"}, say, client, receiving_agent="dave")


# ── handle_app_mention ──────────────────────────────────────────────


class TestHandleAppMention:
    """Tests for the app_mention event handler."""

    @pytest.mark.asyncio
    async def test_app_mention_delegates_to_handle_event(self, app_module):
        """handle_app_mention should delegate to _handle_event with was_mentioned=True."""
        event = {
            "text": "<@UBOT> help",
            "channel": "C001",
            "user": "U001",
            "ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()

        with patch("router.slack_events._handle_event", new_callable=AsyncMock) as mock_handler:
            await app_module.handle_app_mention(event, say, client, receiving_agent="lisa")
            mock_handler.assert_called_once_with(event, say, client, receiving_agent="lisa", was_mentioned=True)


# ── _session_cleanup_loop ───────────────────────────────────────────


class TestSessionCleanupLoop:
    """Tests for the session cleanup loop."""

    @pytest.mark.asyncio
    async def test_cleanup_loop_processes_expired_sessions(self, app_module):
        """Cleanup loop should process timed-out sessions."""
        expired_session = {
            "session_id": "s1",
            "agent_name": "lisa",
            "thread_history": [{"user": "U001", "text": "hi"}],
            "channel": "C001",
            "thread_ts": "1.0",
        }

        # Ensure the cleanup loop can find a Slack client for "lisa".
        app_module._apps_by_agent["lisa"] = MagicMock(client=MagicMock())

        call_count = 0

        async def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise KeyboardInterrupt("break loop")

        try:
            with (
                patch("router.app.asyncio.sleep", side_effect=mock_sleep),
                patch("router.session_lifecycle.pop_timed_out_sessions", return_value=[expired_session]),
                patch("router.session_lifecycle.handle_timeout_exit", new_callable=AsyncMock) as mock_timeout_exit,
                patch(
                    "router.session_lifecycle.get_agent_map",
                    return_value={"lisa": {"container": "lisa", "name": "Lisa"}},
                ),
            ):
                with pytest.raises(KeyboardInterrupt):
                    await app_module._session_cleanup_loop(interval_seconds=1)

                mock_timeout_exit.assert_called_once()
        finally:
            app_module._apps_by_agent.clear()

    @pytest.mark.asyncio
    async def test_cleanup_loop_skips_unknown_agent(self, app_module):
        """Cleanup loop should skip sessions with unknown agents."""
        expired_session = {
            "session_id": "s1",
            "agent_name": "unknown_agent",
            "thread_history": [],
            "channel": "C001",
            "thread_ts": "1.0",
        }

        call_count = 0

        async def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise KeyboardInterrupt("break loop")

        with (
            patch("router.app.asyncio.sleep", side_effect=mock_sleep),
            patch("router.session_lifecycle.pop_timed_out_sessions", return_value=[expired_session]),
            patch("router.session_lifecycle.handle_timeout_exit", new_callable=AsyncMock) as mock_exit,
            patch("router.session_lifecycle.get_agent_map", return_value={"lisa": {"container": "lisa"}}),
        ):
            with pytest.raises(KeyboardInterrupt):
                await app_module._session_cleanup_loop(interval_seconds=1)

            mock_exit.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_loop_handles_timeout_exit_error(self, app_module):
        """Cleanup loop should continue if handle_timeout_exit raises."""
        expired_session = {
            "session_id": "s1",
            "agent_name": "lisa",
            "thread_history": [],
            "channel": "C001",
            "thread_ts": "1.0",
        }

        app_module._apps_by_agent["lisa"] = MagicMock(client=MagicMock())

        call_count = 0

        async def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise KeyboardInterrupt("break loop")

        try:
            with (
                patch("router.app.asyncio.sleep", side_effect=mock_sleep),
                patch("router.session_lifecycle.pop_timed_out_sessions", return_value=[expired_session]),
                patch(
                    "router.session_lifecycle.handle_timeout_exit",
                    new_callable=AsyncMock,
                    side_effect=Exception("exit error"),
                ),
                patch("router.session_lifecycle.get_agent_map", return_value={"lisa": {"container": "lisa"}}),
            ):
                with pytest.raises(KeyboardInterrupt):
                    await app_module._session_cleanup_loop(interval_seconds=1)
        finally:
            app_module._apps_by_agent.clear()

    @pytest.mark.asyncio
    async def test_cleanup_loop_handles_outer_exception(self, app_module):
        """Cleanup loop should survive exceptions in pop_timed_out_sessions."""
        call_count = 0

        async def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise KeyboardInterrupt("break loop")

        with (
            patch("router.app.asyncio.sleep", side_effect=mock_sleep),
            patch("router.session_lifecycle.pop_timed_out_sessions", side_effect=Exception("db error")),
            patch("router.session_lifecycle.get_agent_map", return_value={"lisa": {"container": "lisa"}}),
        ):
            with pytest.raises(KeyboardInterrupt):
                await app_module._session_cleanup_loop(interval_seconds=1)

    @pytest.mark.asyncio
    async def test_cleanup_loop_no_expired_sessions(self, app_module):
        """Cleanup loop with no expired sessions should be a no-op iteration."""
        call_count = 0

        async def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise KeyboardInterrupt("break loop")

        with (
            patch("router.app.asyncio.sleep", side_effect=mock_sleep),
            patch("router.session_lifecycle.pop_timed_out_sessions", return_value=[]),
            patch("router.session_lifecycle.get_agent_map", return_value={"lisa": {"container": "lisa"}}),
        ):
            with pytest.raises(KeyboardInterrupt):
                await app_module._session_cleanup_loop(interval_seconds=1)


# ── main ────────────────────────────────────────────────────────────


class TestMain:
    """Tests for the main entry point."""

    @pytest.mark.asyncio
    async def test_main_starts_socket_mode(self, app_module):
        """main() should start a Socket Mode handler for every configured agent."""

        def _close_coro(coro, **_kwargs):
            """Close coroutines passed to create_task to avoid unawaited warnings."""
            coro.close()
            return MagicMock()

        def _noop_handlers(**_kwargs):
            return None

        # Stub a single agent's app + app token.
        mock_app = MagicMock()
        mock_app.client.auth_test = AsyncMock(return_value={"user_id": "U_BOT_LISA"})
        app_module._apps_by_agent.clear()
        app_module._app_tokens_by_agent.clear()
        app_module._apps_by_agent["lisa"] = mock_app
        app_module._app_tokens_by_agent["lisa"] = "xapp-test"

        try:
            with (
                patch("router.app.AsyncSocketModeHandler") as mock_handler_cls,
                patch("router.app.asyncio.create_task", side_effect=_close_coro),
                patch("router.app.open_store"),
                patch("router.app.start_scheduled_tasks_scheduler") as mock_start_scheduler,
                patch("router.app.setup_scheduled_tasks_handlers", side_effect=_noop_handlers) as mock_setup_tasks,
                patch("router.app.start_healthz_server", AsyncMock(return_value=MagicMock())),
            ):
                mock_handler = MagicMock()
                mock_handler.start_async = AsyncMock()
                mock_handler_cls.return_value = mock_handler

                await app_module.main()

                mock_handler_cls.assert_called_once_with(mock_app, "xapp-test")
                mock_handler.start_async.assert_called_once()
                # Every agent's command is registered on every bolt_app so Socket
                # Mode delivery between sibling sockets always lands on a handler.
                command_names = mock_setup_tasks.call_args.kwargs["command_name"]
                assert isinstance(command_names, list)
                assert "/lisa-tasks" in command_names
                assert all(name.endswith("-tasks") and not name.startswith("/dev-") for name in command_names)
                # Exactly one global scheduler — not one per Bolt app.
                mock_start_scheduler.assert_called_once()
                # Issue #270: dispatch-supervision system tasks are routed
                # through the workers-bot resolver so runtime posts share one
                # identity; agent cron tasks keep their per-agent client.
                assert mock_start_scheduler.call_args.kwargs["system_client_resolver"] is app_module._system_task_client
            assert app_module._bot_user_map["U_BOT_LISA"] == "lisa"
        finally:
            app_module._apps_by_agent.clear()
            app_module._app_tokens_by_agent.clear()
            app_module._bot_user_map.clear()
            app_module._bot_user_id_by_agent.clear()

    @pytest.mark.asyncio
    async def test_main_applies_slash_command_prefix(self, app_module, monkeypatch):
        """SLASH_COMMAND_PREFIX prefixes the per-agent command name (dev/prod coexistence)."""

        def _close_coro(coro, **_kwargs):
            coro.close()
            return MagicMock()

        def _noop_handlers(**_kwargs):
            return None

        monkeypatch.setenv("SLASH_COMMAND_PREFIX", "dev-")

        mock_app = MagicMock()
        mock_app.client.auth_test = AsyncMock(return_value={"user_id": "U_BOT_LISA"})
        app_module._apps_by_agent.clear()
        app_module._app_tokens_by_agent.clear()
        app_module._apps_by_agent["lisa"] = mock_app
        app_module._app_tokens_by_agent["lisa"] = "xapp-test"

        try:
            with (
                patch("router.app.AsyncSocketModeHandler") as mock_handler_cls,
                patch("router.app.asyncio.create_task", side_effect=_close_coro),
                patch("router.app.open_store"),
                patch("router.app.start_scheduled_tasks_scheduler"),
                patch("router.app.setup_scheduled_tasks_handlers", side_effect=_noop_handlers) as mock_setup_tasks,
                patch("router.app.start_healthz_server", AsyncMock(return_value=MagicMock())),
            ):
                mock_handler_cls.return_value = MagicMock(start_async=AsyncMock())
                await app_module.main()
                command_names = mock_setup_tasks.call_args.kwargs["command_name"]
                assert isinstance(command_names, list)
                assert "/dev-lisa-tasks" in command_names
                assert all(name.startswith("/dev-") and name.endswith("-tasks") for name in command_names)
        finally:
            app_module._apps_by_agent.clear()
            app_module._app_tokens_by_agent.clear()
            app_module._bot_user_map.clear()
            app_module._bot_user_id_by_agent.clear()

    @pytest.mark.asyncio
    async def test_resolver_derives_agent_from_command_body(self, app_module, monkeypatch):
        """The resolver passed to setup_scheduled_tasks must read body['command'].

        Socket Mode load-balances between sibling sockets that share a Slack
        App, so Lisa's bolt_app may receive ``/dev-sam-tasks`` and vice versa.
        The resolver must therefore key off the command itself, not the
        bolt_app it was registered on.
        """

        def _close_coro(coro, **_kwargs):
            coro.close()
            return MagicMock()

        captured: dict = {}

        def _capture_setup(**kwargs):
            captured["resolver"] = kwargs["agent_resolver"]
            return None

        monkeypatch.setenv("SLASH_COMMAND_PREFIX", "dev-")

        mock_app = MagicMock()
        mock_app.client.auth_test = AsyncMock(return_value={"user_id": "U_BOT_LISA"})
        app_module._apps_by_agent.clear()
        app_module._app_tokens_by_agent.clear()
        app_module._apps_by_agent["lisa"] = mock_app
        app_module._app_tokens_by_agent["lisa"] = "xapp-test"

        try:
            with (
                patch("router.app.AsyncSocketModeHandler") as mock_handler_cls,
                patch("router.app.asyncio.create_task", side_effect=_close_coro),
                patch("router.app.open_store"),
                patch("router.app.start_scheduled_tasks_scheduler"),
                patch("router.app.setup_scheduled_tasks_handlers", side_effect=_capture_setup),
                patch("router.app.start_healthz_server", AsyncMock(return_value=MagicMock())),
            ):
                mock_handler_cls.return_value = MagicMock(start_async=AsyncMock())
                await app_module.main()

            resolver = captured["resolver"]
            assert resolver({"command": "/dev-lisa-tasks"}) == "lisa"
            assert resolver({"command": "/dev-sam-tasks"}) == "sam"
            # Commands that don't end in -tasks aren't ours.
            assert resolver({"command": "/dev-something-else"}) is None
            assert resolver({"command": ""}) is None
            assert resolver({}) is None
        finally:
            app_module._apps_by_agent.clear()
            app_module._app_tokens_by_agent.clear()
            app_module._bot_user_map.clear()
            app_module._bot_user_id_by_agent.clear()

    @pytest.mark.asyncio
    async def test_main_auto_seeds_workers_bot_id(self, app_module, monkeypatch):
        """With WORKERS_BOT_TOKEN set, the workers bot user id is resolved via
        auth.test and lands in the global dispatch allowlist consulted by every
        agent app's bot-message guard (#252)."""

        def _close_coro(coro, **_kwargs):
            coro.close()
            return MagicMock()

        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-workers-test")
        monkeypatch.delenv("DISPATCH_BOT_USER_IDS", raising=False)

        mock_app = MagicMock()
        mock_app.client.auth_test = AsyncMock(return_value={"user_id": "U_BOT_LISA"})
        app_module._apps_by_agent.clear()
        app_module._app_tokens_by_agent.clear()
        app_module._dispatch_bot_user_ids.clear()
        app_module._apps_by_agent["lisa"] = mock_app
        app_module._app_tokens_by_agent["lisa"] = "xapp-test"

        mock_workers_client = MagicMock()
        mock_workers_client.auth_test = AsyncMock(return_value={"user_id": "U_BOT_WORKERS"})

        try:
            with (
                patch("router.app.AsyncSocketModeHandler") as mock_handler_cls,
                patch("router.app.asyncio.create_task", side_effect=_close_coro),
                patch("router.app.open_store"),
                patch("router.app.start_scheduled_tasks_scheduler"),
                patch("router.app.setup_scheduled_tasks_handlers", side_effect=lambda **_k: None),
                patch("router.app.start_healthz_server", AsyncMock(return_value=MagicMock())),
                patch("router.app.AsyncWebClient", return_value=mock_workers_client) as mock_web_cls,
            ):
                mock_handler_cls.return_value = MagicMock(start_async=AsyncMock())
                await app_module.main()

            # auth.test ran against the workers token, no agent token reuse.
            mock_web_cls.assert_called_once_with(token="xoxb-workers-test")
            mock_workers_client.auth_test.assert_awaited_once()
            # Workers bot id reaches the global allowlist and the per-identity sentinel.
            assert "U_BOT_WORKERS" in app_module._dispatch_bot_user_ids
            assert router_runtime.workers_bot_user_id == "U_BOT_WORKERS"
            # Guard still drops plain workers-bot posts when WORKER_MENTION_HANDOFF=0
            # (default).  The narrow carve-out (issue #283) requires the flag + mention.
            assert not app_module._is_dispatch_bot_sender({"user": "U_BOT_WORKERS", "text": ""}, "lisa")
        finally:
            app_module._apps_by_agent.clear()
            app_module._app_tokens_by_agent.clear()
            app_module._bot_user_map.clear()
            app_module._bot_user_id_by_agent.clear()
            app_module._dispatch_bot_user_ids.clear()

    @pytest.mark.asyncio
    async def test_main_skips_workers_seed_when_token_unset(self, app_module, isolated_settings, monkeypatch):
        """No WORKERS_BOT_TOKEN and no secrets.json entry → main() runs cleanly,
        the workers client is never constructed, and no workers entry is seeded
        into the allowlist (#252)."""

        def _close_coro(coro, **_kwargs):
            coro.close()
            return MagicMock()

        monkeypatch.delenv("WORKERS_BOT_TOKEN", raising=False)
        monkeypatch.delenv("DISPATCH_BOT_USER_IDS", raising=False)

        mock_app = MagicMock()
        mock_app.client.auth_test = AsyncMock(return_value={"user_id": "U_BOT_LISA"})
        app_module._apps_by_agent.clear()
        app_module._app_tokens_by_agent.clear()
        app_module._dispatch_bot_user_ids.clear()
        app_module._apps_by_agent["lisa"] = mock_app
        app_module._app_tokens_by_agent["lisa"] = "xapp-test"

        try:
            with (
                patch("router.app.AsyncSocketModeHandler") as mock_handler_cls,
                patch("router.app.asyncio.create_task", side_effect=_close_coro),
                patch("router.app.open_store"),
                patch("router.app.start_scheduled_tasks_scheduler"),
                patch("router.app.setup_scheduled_tasks_handlers", side_effect=lambda **_k: None),
                patch("router.app.start_healthz_server", AsyncMock(return_value=MagicMock())),
                patch("router.app.AsyncWebClient") as mock_web_cls,
            ):
                mock_handler_cls.return_value = MagicMock(start_async=AsyncMock())
                await app_module.main()

            mock_web_cls.assert_not_called()
            # Allowlist holds only the agent's own auto-seeded id — no workers entry.
            assert app_module._dispatch_bot_user_ids == {"U_BOT_LISA"}
        finally:
            app_module._apps_by_agent.clear()
            app_module._app_tokens_by_agent.clear()
            app_module._bot_user_map.clear()
            app_module._bot_user_id_by_agent.clear()
            app_module._dispatch_bot_user_ids.clear()

    @pytest.mark.asyncio
    async def test_resolve_workers_bot_user_id_reads_from_secret_store(
        self, app_module, isolated_settings, monkeypatch
    ):
        """Issue #292: env absent → fall through to SecretStore; seed succeeds when
        workers_bot_token is present only in secrets.json."""
        monkeypatch.delenv("WORKERS_BOT_TOKEN", raising=False)
        isolated_settings.set_str("workers_bot_token", "xoxb-from-secrets-json")

        mock_workers_client = MagicMock()
        mock_workers_client.auth_test = AsyncMock(return_value={"user_id": "U_BOT_WORKERS_STORE"})

        with patch("router.app.AsyncWebClient", return_value=mock_workers_client) as mock_web_cls:
            result = await app_module._resolve_workers_bot_user_id()

        mock_web_cls.assert_called_once_with(token="xoxb-from-secrets-json")
        assert result == "U_BOT_WORKERS_STORE"

    @pytest.mark.asyncio
    async def test_main_degrades_when_workers_auth_test_fails(self, app_module, monkeypatch, caplog):
        """auth.test failure for the workers token logs a warning with the Slack
        error code and continues startup — no crash, no workers entry (#252)."""
        from slack_sdk.errors import SlackApiError

        def _close_coro(coro, **_kwargs):
            coro.close()
            return MagicMock()

        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-workers-bad")
        monkeypatch.delenv("DISPATCH_BOT_USER_IDS", raising=False)

        mock_app = MagicMock()
        mock_app.client.auth_test = AsyncMock(return_value={"user_id": "U_BOT_LISA"})
        app_module._apps_by_agent.clear()
        app_module._app_tokens_by_agent.clear()
        app_module._dispatch_bot_user_ids.clear()
        app_module._apps_by_agent["lisa"] = mock_app
        app_module._app_tokens_by_agent["lisa"] = "xapp-test"

        mock_workers_client = MagicMock()
        mock_workers_client.auth_test = AsyncMock(
            side_effect=SlackApiError("invalid_auth", response={"error": "invalid_auth"})
        )

        try:
            with (
                patch("router.app.AsyncSocketModeHandler") as mock_handler_cls,
                patch("router.app.asyncio.create_task", side_effect=_close_coro),
                patch("router.app.open_store"),
                patch("router.app.start_scheduled_tasks_scheduler"),
                patch("router.app.setup_scheduled_tasks_handlers", side_effect=lambda **_k: None),
                patch("router.app.start_healthz_server", AsyncMock(return_value=MagicMock())),
                patch("router.app.AsyncWebClient", return_value=mock_workers_client),
                caplog.at_level("WARNING", logger="router.app"),
            ):
                mock_handler_cls.return_value = MagicMock(start_async=AsyncMock())
                await app_module.main()

            # Startup survived; only the agent's own id is whitelisted.
            assert app_module._dispatch_bot_user_ids == {"U_BOT_LISA"}
            assert "invalid_auth" in caplog.text
        finally:
            app_module._apps_by_agent.clear()
            app_module._app_tokens_by_agent.clear()
            app_module._bot_user_map.clear()
            app_module._bot_user_id_by_agent.clear()
            app_module._dispatch_bot_user_ids.clear()


# ── dispatch thread routing (#173) ──────────────────────────────────


class TestDispatchThreadRouting:
    """@-mentions and follow-ups inside dispatch threads route to the agent's
    normal Slack session regardless of whether a dispatch worker is in-flight.
    Issue #173: router was not re-entering agent session for dispatch threads."""

    @pytest.mark.asyncio
    async def test_app_mention_in_dispatch_thread_no_worker_invokes_agent(self, app_module, tmp_path):
        """An @-mention in a dispatch thread with no running worker invokes the agent.

        'message in dispatch thread, agent mentioned, no in-flight worker'
        → agent invoked (acceptance criterion from #173).
        """
        event = {
            "text": "<@U_BOT_SAM> what's the progress?",
            "channel": "C001",
            "user": "U001",
            "ts": "2.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()

        with (
            patch(
                "router.slack_events.get_agent_map",
                return_value={"sam": {"container": "sam", "name": "Sam"}},
            ),
            patch("router.slack_events.find_session_by_thread", return_value=None),
            patch(
                "router.slack_events.create_session",
                return_value={"session_id": "s1", "agent_name": "sam"},
            ),
            patch(
                "router.slack_events.dispatch",
                new_callable=AsyncMock,
                return_value={"response": "Still working on it."},
            ) as mock_dispatch,
            patch("router.slack_events.update_activity"),
            patch("router.slack_events.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client, receiving_agent="sam", was_mentioned=True)

        mock_dispatch.assert_called_once()
        assert mock_dispatch.call_args.kwargs["agent_name"] == "sam"
        say.assert_called_once()

    @pytest.mark.asyncio
    async def test_app_mention_in_dispatch_thread_worker_running_invokes_agent(self, app_module, tmp_path):
        """An @-mention in a dispatch thread with a running worker invokes the agent
        and leaves the dispatch worker's state files untouched.

        'message in dispatch thread, agent mentioned, worker running'
        → agent invoked, worker untouched (acceptance criterion from #173).
        """
        import os

        # Set up an in-flight dispatch state (no exitcode = still running).
        dispatch_id = "dispatch-20260101T000000-abc123"
        workspace = tmp_path / dispatch_id
        workspace.mkdir()
        (workspace / "channel").write_text("C001")
        (workspace / "thread_ts").write_text("1.0")
        (workspace / "agent").write_text("sam")
        (workspace / "pid").write_text("99999")
        # No exitcode file — dispatch is still running.

        state_files_before = {f: (workspace / f).read_text() for f in ("channel", "thread_ts", "agent", "pid")}

        event = {
            "text": "<@U_BOT_SAM> what's happening?",
            "channel": "C001",
            "user": "U001",
            "ts": "2.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()

        with (
            patch(
                "router.slack_events.get_agent_map",
                return_value={"sam": {"container": "sam", "name": "Sam"}},
            ),
            patch("router.slack_events.find_session_by_thread", return_value=None),
            patch(
                "router.slack_events.create_session",
                return_value={"session_id": "s1", "agent_name": "sam"},
            ),
            patch(
                "router.slack_events.dispatch",
                new_callable=AsyncMock,
                return_value={"response": "Dispatch is running…"},
            ) as mock_dispatch,
            patch("router.slack_events.update_activity"),
            patch("router.slack_events.add_to_thread_history"),
            # Point dispatch state at our tmp workspace root.
            patch.dict(os.environ, {"DISPATCH_WORKSPACE_ROOT": str(tmp_path)}),
        ):
            await app_module._handle_event(event, say, client, receiving_agent="sam", was_mentioned=True)

        # Agent's normal session was invoked — not the dispatch worker.
        mock_dispatch.assert_called_once()
        assert mock_dispatch.call_args.kwargs["agent_name"] == "sam"
        say.assert_called_once()

        # Worker state files are completely untouched.
        state_files_after = {f: (workspace / f).read_text() for f in ("channel", "thread_ts", "agent", "pid")}
        assert state_files_before == state_files_after, (
            "dispatch worker state was modified — worker must remain isolated"
        )
        assert not (workspace / "exitcode").exists(), (
            "dispatch worker exitcode was written — worker must not be interrupted"
        )

    @pytest.mark.asyncio
    async def test_unmentioned_reply_in_dispatch_thread_with_no_active_agent(self, app_module, tmp_path):
        """An unmentioned reply in a dispatch thread routes to the owning agent
        even when no active_agent is recorded for the thread.

        This covers the case where the dispatch was initiated before
        thread-state was established (e.g. dispatch triggered from a pack
        command, not a direct @-mention).
        """
        import os

        # In-flight dispatch for (C001, "1.0") owned by sam.
        dispatch_id = "dispatch-20260101T000001-def456"
        workspace = tmp_path / dispatch_id
        workspace.mkdir()
        (workspace / "channel").write_text("C001")
        (workspace / "thread_ts").write_text("1.0")
        (workspace / "agent").write_text("sam")
        (workspace / "pid").write_text("99998")

        # No active_agent set for this thread — store is fresh.
        event = {
            "channel_type": "channel",
            "text": "any news?",
            "channel": "C001",
            "user": "U001",
            "ts": "2.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()

        with (
            patch(
                "router.slack_events.get_agent_map",
                return_value={"sam": {"container": "sam", "name": "Sam"}},
            ),
            patch("router.slack_events.find_session_by_thread", return_value=None),
            patch(
                "router.slack_events.create_session",
                return_value={"session_id": "s1", "agent_name": "sam"},
            ),
            patch(
                "router.slack_events.dispatch",
                new_callable=AsyncMock,
                return_value={"response": "Still going…"},
            ) as mock_dispatch,
            patch("router.slack_events.update_activity"),
            patch("router.slack_events.add_to_thread_history"),
            patch.dict(os.environ, {"DISPATCH_WORKSPACE_ROOT": str(tmp_path)}),
        ):
            await app_module.handle_message(event, say, client, receiving_agent="sam")

        mock_dispatch.assert_called_once()
        assert mock_dispatch.call_args.kwargs["agent_name"] == "sam"

    @pytest.mark.asyncio
    async def test_dispatch_thread_and_direct_mention_thread_route_identically(self, app_module, tmp_path):
        """Threads created by dispatch and threads created by direct mention
        behave identically for inbound routing (acceptance criterion from #173).

        Both scenarios call _handle_event with the same receiving_agent;
        neither is blocked or treated differently by the routing layer.
        """
        import os

        say = AsyncMock()
        client = AsyncMock()

        # Direct-mention thread: @-mention received via app_mention.
        direct_mention_event = {
            "text": "<@U_BOT_SAM> help",
            "channel": "C002",
            "user": "U001",
            "ts": "10.0",
            "thread_ts": "10.0",
        }
        with (
            patch(
                "router.slack_events.get_agent_map",
                return_value={"sam": {"container": "sam", "name": "Sam"}},
            ),
            patch("router.slack_events.find_session_by_thread", return_value=None),
            patch(
                "router.slack_events.create_session",
                return_value={"session_id": "s1", "agent_name": "sam"},
            ),
            patch(
                "router.slack_events.dispatch",
                new_callable=AsyncMock,
                return_value={"response": "On it."},
            ) as mock_dispatch_direct,
            patch("router.slack_events.update_activity"),
            patch("router.slack_events.add_to_thread_history"),
        ):
            await app_module._handle_event(direct_mention_event, say, client, receiving_agent="sam", was_mentioned=True)
        mock_dispatch_direct.assert_called_once()

        # Dispatch thread: @-mention received via app_mention (same path).
        dispatch_id = "dispatch-20260101T000002-ghi789"
        workspace = tmp_path / dispatch_id
        workspace.mkdir()
        (workspace / "channel").write_text("C001")
        (workspace / "thread_ts").write_text("1.0")
        (workspace / "agent").write_text("sam")
        (workspace / "pid").write_text("99997")

        dispatch_mention_event = {
            "text": "<@U_BOT_SAM> what's happening?",
            "channel": "C001",
            "user": "U001",
            "ts": "2.0",
            "thread_ts": "1.0",
        }
        say2 = AsyncMock()
        with (
            patch(
                "router.slack_events.get_agent_map",
                return_value={"sam": {"container": "sam", "name": "Sam"}},
            ),
            patch("router.slack_events.find_session_by_thread", return_value=None),
            patch(
                "router.slack_events.create_session",
                return_value={"session_id": "s1", "agent_name": "sam"},
            ),
            patch(
                "router.slack_events.dispatch",
                new_callable=AsyncMock,
                return_value={"response": "Dispatch running."},
            ) as mock_dispatch_dispatch,
            patch("router.slack_events.update_activity"),
            patch("router.slack_events.add_to_thread_history"),
            patch.dict(os.environ, {"DISPATCH_WORKSPACE_ROOT": str(tmp_path)}),
        ):
            await app_module._handle_event(
                dispatch_mention_event, say2, client, receiving_agent="sam", was_mentioned=True
            )
        mock_dispatch_dispatch.assert_called_once()
        assert (
            mock_dispatch_dispatch.call_args.kwargs["agent_name"] == mock_dispatch_direct.call_args.kwargs["agent_name"]
        )


# ── _execute_approved_draft ─────────────────────────────────────────


class TestExecuteApprovedDraft:
    """Tests for the _execute_approved_draft callback (issue #212)."""

    def _make_draft(self, capability_instance: str, action_verb: str, payload: dict | None = None):
        from router.approvals.store import Draft

        return Draft(
            draft_id="test-draft-001",
            agent_name="lisa",
            capability_type="pack",
            capability_instance=capability_instance,
            action_verb=action_verb,
            payload=payload or {},
            slack_channel="C001",
            slack_message_ts="1.0",
        )

    @pytest.mark.asyncio
    async def test_execute_approved_draft_dispatch_runs_via_docker_exec(self, app_module):
        """Approving a dispatch_issue draft must shell out to docker exec on the
        originating agent's container — never call dispatch_issue() in-process
        inside the router container (issue #219)."""
        import json as _json

        draft = self._make_draft(
            "dispatch",
            "dispatch_issue",
            {"issue_url": "https://github.com/org/repo/issues/1"},
        )
        client = AsyncMock()

        run_result = (_json.dumps({"status": "launched", "dispatch_id": "dispatch-abc123"}), "", 0)
        with (
            patch("router.approvals.execute._workers_client", return_value=None),
            patch(
                "router.approvals.execute.get_agent_map",
                return_value={"lisa": {"container": "lisa-container", "name": "Lisa"}},
            ),
            patch(
                "router.approvals.execute._run_in_container",
                new_callable=AsyncMock,
                return_value=run_result,
            ) as mock_run,
        ):
            await app_module._execute_approved_draft(draft, "C001", "1.0", client)

        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs["container"] == "lisa-container"
        cmd = call_kwargs["command"]
        assert "dispatch_issue" in cmd
        assert "--issue-url" in cmd
        assert "https://github.com/org/repo/issues/1" in cmd
        assert "--approved" in cmd
        # Issue #212 (post-#221): must force poll supervision so the
        # handler doesn't fall back to inline and exceed the 120 s docker
        # exec timeout.
        assert "--supervision-mode" in cmd
        assert cmd[cmd.index("--supervision-mode") + 1] == "poll"

        client.chat_postMessage.assert_called_once()
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "dispatch-abc123" in text
        assert "launched" in text

    @pytest.mark.asyncio
    async def test_execute_approved_draft_dispatch_passes_poll_supervision_mode(self, app_module):
        """Regression for issue #212 (post-#221): the docker-exec cmd MUST
        carry ``--supervision-mode poll``. Without it the handler reads
        ``$DISPATCH_SUPERVISION`` from the agent container's env (unset
        in every shipped agent), falls back to inline mode, blocks for
        the full ~30 min worker budget, and times out the docker exec
        after 120 s — leaving an orphaned ``claude -p`` grandchild
        running until the next supervisor sweep reaps it."""
        import json as _json

        draft = self._make_draft(
            "dispatch",
            "dispatch_issue",
            {"issue_url": "https://github.com/org/repo/issues/212"},
        )
        client = AsyncMock()

        run_result = (_json.dumps({"status": "launched", "dispatch_id": "dispatch-poll001"}), "", 0)
        with (
            patch("router.approvals.execute._workers_client", return_value=None),
            patch(
                "router.approvals.execute.get_agent_map",
                return_value={"lisa": {"container": "lisa-container", "name": "Lisa"}},
            ),
            patch(
                "router.approvals.execute._run_in_container",
                new_callable=AsyncMock,
                return_value=run_result,
            ) as mock_run,
        ):
            await app_module._execute_approved_draft(draft, "C001", "1.0", client)

        cmd = mock_run.call_args.kwargs["command"]
        # Pinned ordering: ``--supervision-mode`` must be followed by
        # ``poll``. ``inline`` is the wrong value, missing flag is the
        # wrong value, anything but ``poll`` here regresses to the
        # 120 s-timeout / orphan-worker failure mode.
        assert "--supervision-mode" in cmd, f"missing --supervision-mode in {cmd}"
        idx = cmd.index("--supervision-mode")
        assert cmd[idx + 1] == "poll", f"expected poll after --supervision-mode, got {cmd[idx + 1]!r}"

    @pytest.mark.asyncio
    async def test_execute_approved_draft_dispatch_injects_pack_env(self, app_module):
        """Regression for issue #268: the approval-card execution path MUST
        compute ``pack_cli_extras(...)`` and forward its ``env`` to
        ``_run_in_container``. Without this, ``WORKERS_BOT_TOKEN`` is absent
        from the docker exec, the handler's #257 fail-fast guard fires
        ``workers_token_missing``, and every pilot-mode approval-card
        dispatch errors out before the worker ever spawns.

        The mirror path on ``router/dispatcher.py`` (agent-initiated
        dispatches) already passes ``env=extras.env``; this test pins the
        symmetric contract on the approval-card lane so the two paths
        cannot drift again.
        """
        import json as _json

        draft = self._make_draft(
            "dispatch",
            "dispatch_issue",
            {"issue_url": "https://github.com/org/repo/issues/268"},
        )
        client = AsyncMock()

        run_result = (_json.dumps({"status": "launched", "dispatch_id": "dispatch-env001"}), "", 0)
        from router.packs.dispatch_hook import PackDispatchExtras

        fake_extras = PackDispatchExtras(
            prompt_files=[],
            mcp_config_path=None,
            env={"WORKERS_BOT_TOKEN": "xoxb-fake-token-for-test"},
        )

        with (
            patch("router.approvals.execute._workers_client", return_value=None),
            patch(
                "router.approvals.execute.get_agent_map",
                return_value={"lisa": {"container": "lisa-container", "name": "Lisa"}},
            ),
            patch(
                "router.approvals.execute.pack_cli_extras",
                return_value=fake_extras,
            ) as mock_extras,
            patch(
                "router.approvals.execute._run_in_container",
                new_callable=AsyncMock,
                return_value=run_result,
            ) as mock_run,
        ):
            await app_module._execute_approved_draft(draft, "C001", "1.0", client)

        # The hook must be consulted per dispatch (so future pack secrets
        # land symmetrically across both docker-exec paths).
        mock_extras.assert_called_once()
        extras_kwargs = mock_extras.call_args.kwargs
        assert extras_kwargs.get("channel") == "C001"
        assert extras_kwargs.get("thread_ts") == "1.0"

        # The hook's env must be forwarded as the ``env=`` kwarg, not
        # silently dropped. ``None`` / missing here regresses to the
        # ``workers_token_missing`` failure mode that motivated #268.
        call_kwargs = mock_run.call_args.kwargs
        assert "env" in call_kwargs, f"_run_in_container called without env= kwarg: {call_kwargs!r}"
        env = call_kwargs["env"]
        assert env is not None, "env= forwarded as None — handler #257 guard will fire"
        assert env.get("WORKERS_BOT_TOKEN") == "xoxb-fake-token-for-test", (
            f"WORKERS_BOT_TOKEN missing from forwarded env: {env!r}"
        )

    @pytest.mark.asyncio
    async def test_execute_approved_draft_discord_transport_passes_conversation_ref(self, app_module):
        """#665: when draft.payload carries transport=discord + conversation_id,
        pack_cli_extras must receive the conversation_ref so the worker
        container gets DISPATCH_TRANSPORT=discord / DISPATCH_CONVERSATION_ID
        and posts status back to the originating Discord thread."""
        import json as _json

        discord_ref = "discord:111:222:333"
        draft = self._make_draft(
            "dispatch",
            "dispatch_issue",
            {
                "issue_url": "https://github.com/org/repo/issues/665",
                "transport": "discord",
                "conversation_id": discord_ref,
            },
        )
        client = AsyncMock()

        run_result = (_json.dumps({"status": "launched", "dispatch_id": "dispatch-discord01"}), "", 0)
        from router.packs.dispatch_hook import PackDispatchExtras

        fake_extras = PackDispatchExtras(
            prompt_files=[],
            mcp_config_path=None,
            env={"WORKERS_BOT_TOKEN": "xoxb-fake", "DISPATCH_TRANSPORT": "discord"},
        )

        with (
            patch("router.approvals.execute._workers_client", return_value=None),
            patch(
                "router.approvals.execute.get_agent_map",
                return_value={"lisa": {"container": "lisa-container", "name": "Lisa"}},
            ),
            patch(
                "router.approvals.execute.pack_cli_extras",
                return_value=fake_extras,
            ) as mock_extras,
            patch(
                "router.approvals.execute._run_in_container",
                new_callable=AsyncMock,
                return_value=run_result,
            ),
        ):
            await app_module._execute_approved_draft(draft, "C001", "1.0", client)

        mock_extras.assert_called_once()
        extras_kwargs = mock_extras.call_args.kwargs
        assert extras_kwargs.get("conversation_ref") == discord_ref, (
            f"Expected conversation_ref={discord_ref!r}, got {extras_kwargs.get('conversation_ref')!r}"
        )

    @pytest.mark.asyncio
    async def test_execute_approved_draft_discord_transport_posts_lifecycle_via_passed_client(self, app_module):
        """#682: Discord-origin drafts must post launched/done/error lifecycle
        lines through the passed-in client (a Discord-conversation-bound
        facade), never the Slack workers client — the Slack workers bot has
        no Discord identity, so a Slack chat_postMessage call there would
        either post to the wrong place or error out silently, and the
        approve→execute loop's status line would never land on Discord."""
        import json as _json

        discord_ref = "discord:111:222:333"
        draft = self._make_draft(
            "dispatch",
            "dispatch_issue",
            {
                "issue_url": "https://github.com/org/repo/issues/682",
                "transport": "discord",
                "conversation_id": discord_ref,
            },
        )
        discord_client = AsyncMock()
        slack_workers_client = AsyncMock()

        run_result = (_json.dumps({"status": "launched", "dispatch_id": "dispatch-discord02"}), "", 0)
        from router.packs.dispatch_hook import PackDispatchExtras

        fake_extras = PackDispatchExtras(prompt_files=[], mcp_config_path=None, env={})

        with (
            patch("router.approvals.execute._workers_client", return_value=slack_workers_client),
            patch(
                "router.approvals.execute.get_agent_map",
                return_value={"lisa": {"container": "lisa-container", "name": "Lisa"}},
            ),
            patch("router.approvals.execute.pack_cli_extras", return_value=fake_extras),
            patch(
                "router.approvals.execute._run_in_container",
                new_callable=AsyncMock,
                return_value=run_result,
            ),
        ):
            await app_module._execute_approved_draft(draft, "", "", discord_client)

        discord_client.chat_postMessage.assert_called_once()
        assert "dispatch-discord02" in discord_client.chat_postMessage.call_args.kwargs["text"]
        slack_workers_client.chat_postMessage.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_approved_draft_slack_transport_no_conversation_ref(self, app_module):
        """#665: Slack-origin drafts (transport != discord) must pass
        conversation_ref=None so the Slack path in pack_cli_extras is unaffected."""
        import json as _json

        draft = self._make_draft(
            "dispatch",
            "dispatch_issue",
            {
                "issue_url": "https://github.com/org/repo/issues/665",
                "transport": "slack",
                "conversation_id": "C001|1.0",
            },
        )
        client = AsyncMock()

        run_result = (_json.dumps({"status": "launched", "dispatch_id": "dispatch-slack01"}), "", 0)
        from router.packs.dispatch_hook import PackDispatchExtras

        fake_extras = PackDispatchExtras(prompt_files=[], mcp_config_path=None, env={})

        with (
            patch("router.approvals.execute._workers_client", return_value=None),
            patch(
                "router.approvals.execute.get_agent_map",
                return_value={"lisa": {"container": "lisa-container", "name": "Lisa"}},
            ),
            patch(
                "router.approvals.execute.pack_cli_extras",
                return_value=fake_extras,
            ) as mock_extras,
            patch(
                "router.approvals.execute._run_in_container",
                new_callable=AsyncMock,
                return_value=run_result,
            ),
        ):
            await app_module._execute_approved_draft(draft, "C001", "1.0", client)

        mock_extras.assert_called_once()
        extras_kwargs = mock_extras.call_args.kwargs
        assert extras_kwargs.get("conversation_ref") is None, (
            f"Slack path must not set conversation_ref, got {extras_kwargs.get('conversation_ref')!r}"
        )

    @pytest.mark.asyncio
    async def test_execute_approved_draft_non_dispatch_falls_back_to_cli(self, app_module):
        """Approving a non-dispatch draft (e.g. github pr_merge) must use
        the existing agent CLI re-entry path via dispatch()."""
        draft = self._make_draft("github", "pr_merge", {"pr_url": "https://github.com/org/repo/pull/42"})
        client = AsyncMock()

        with (
            patch(
                "router.approvals.execute.get_agent_map",
                return_value={"lisa": {"container": "lisa", "name": "Lisa"}},
            ),
            patch(
                "router.approvals.execute.dispatch",
                new_callable=AsyncMock,
                return_value={"response": "PR merged!"},
            ) as mock_dispatch,
        ):
            await app_module._execute_approved_draft(draft, "C001", "1.0", client)

        mock_dispatch.assert_called_once()
        assert mock_dispatch.call_args.kwargs["agent_name"] == "lisa"

    @pytest.mark.asyncio
    async def test_execute_approved_draft_dispatch_handler_error_posts_to_slack(self, app_module):
        """When docker exec dispatch_issue returns {status: error, reason: ...},
        the reason must appear in the Slack post."""
        import json as _json

        draft = self._make_draft(
            "dispatch",
            "dispatch_issue",
            {"issue_url": "https://github.com/org/repo/issues/2"},
        )
        client = AsyncMock()

        run_result = (
            _json.dumps({"status": "error", "reason": "auth_seed_failed", "dispatch_id": "dispatch-err001"}),
            "",
            1,
        )
        with (
            patch("router.approvals.execute._workers_client", return_value=None),
            patch(
                "router.approvals.execute.get_agent_map",
                return_value={"lisa": {"container": "lisa-container", "name": "Lisa"}},
            ),
            patch(
                "router.approvals.execute._run_in_container",
                new_callable=AsyncMock,
                return_value=run_result,
            ),
        ):
            await app_module._execute_approved_draft(draft, "C001", "1.0", client)

        client.chat_postMessage.assert_called_once()
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "auth_seed_failed" in text

    @pytest.mark.asyncio
    async def test_execute_approved_draft_dispatch_accepts_real_gate_preview_payload(self, app_module, tmp_path):
        """Regression for #216: draft.payload is produced by
        ``packs.dispatch.handler._evaluate_approval_gate`` — NOT a hand-crafted
        dict matching ``dispatch_issue()``'s kwarg signature. The executor must
        map the producer's real shape (no ``channel``/``thread_ts``/``agent``
        keys; with extra keys like ``repo``/``branch_target``/``est_workspace_path``)
        without raising TypeError on the splat.

        The previous test suite hand-built a fictional payload that happened to
        match the handler's kwargs, which is why CI passed while every real
        approval blew up with a 600 s timeout in production.
        """
        from datetime import datetime, timezone

        from packs.dispatch.handler import _evaluate_approval_gate

        # Drive _evaluate_approval_gate with require_always=True so it
        # deterministically returns a preview (no cost lookup, no fetch).
        gate_preview = _evaluate_approval_gate(
            issue_url="https://github.com/org/repo/issues/216",
            model="sonnet",
            root=tmp_path,
            now=datetime(2026, 5, 20, tzinfo=timezone.utc),
            approval_cfg={"require_always": True},
            cost_threshold=15.0,
        )
        assert gate_preview is not None, "gate must fire under require_always=True"
        # Lock the producer's surface — if these keys drift, this test fails
        # and forces a deliberate update to the executor mapping above.
        assert set(gate_preview.keys()) == {
            "repo",
            "issue_url",
            "branch_target",
            "model",
            "est_workspace_path",
            "gate_reason",
        }

        # Mirror the store/_persist flow: the gate preview *is* what gets
        # written into draft.payload.
        draft = self._make_draft("dispatch", "dispatch_issue", dict(gate_preview))
        client = AsyncMock()

        import json as _json

        run_result = (_json.dumps({"status": "launched", "dispatch_id": "dispatch-real001"}), "", 0)
        with (
            patch("router.approvals.execute._workers_client", return_value=None),
            patch(
                "router.approvals.execute.get_agent_map",
                return_value={"lisa": {"container": "lisa-container", "name": "Lisa"}},
            ),
            patch(
                "router.approvals.execute._run_in_container",
                new_callable=AsyncMock,
                return_value=run_result,
            ) as mock_run,
        ):
            await app_module._execute_approved_draft(draft, "C-thread", "1700000000.123456", client)

        # docker exec command must carry issue_url + model from payload, but NOT
        # the extra gate-preview keys (repo, branch_target, est_workspace_path,
        # gate_reason) — those are for the human preview only.
        mock_run.assert_called_once()
        cmd = mock_run.call_args.kwargs["command"]
        assert "--issue-url" in cmd
        assert "https://github.com/org/repo/issues/216" in cmd
        assert "--channel" in cmd
        assert "C-thread" in cmd
        assert "--thread-ts" in cmd
        assert "1700000000.123456" in cmd
        assert "--agent" in cmd
        assert "lisa" in cmd
        assert "--model" in cmd
        assert "sonnet" in cmd
        assert "--approved" in cmd
        # Extra payload keys must not appear as CLI flags
        for bad_flag in ("--repo", "--branch-target", "--est-workspace-path", "--gate-reason"):
            assert bad_flag not in cmd

        client.chat_postMessage.assert_called_once()
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "dispatch-real001" in text
        assert "launched" in text

    @pytest.mark.asyncio
    async def test_execute_approved_draft_dispatch_missing_issue_url_posts_to_slack(self, app_module):
        """A malformed payload missing issue_url must surface as a Slack error,
        not silently no-op or raise. Guards against future regressions in the
        gate preview producer."""
        draft = self._make_draft("dispatch", "dispatch_issue", {"model": "sonnet"})
        client = AsyncMock()

        with patch("router.approvals.execute._workers_client", return_value=None):
            await app_module._execute_approved_draft(draft, "C001", "1.0", client)

        client.chat_postMessage.assert_called_once()
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "missing issue_url" in text

    @pytest.mark.asyncio
    async def test_execute_approved_draft_dispatch_unknown_agent_posts_to_slack(self, app_module):
        """When draft.agent_name is not in the agent map, post a Slack error
        without attempting docker exec."""
        draft = self._make_draft(
            "dispatch",
            "dispatch_issue",
            {"issue_url": "https://github.com/org/repo/issues/99"},
        )
        client = AsyncMock()

        with (
            patch("router.approvals.execute._workers_client", return_value=None),
            patch("router.approvals.execute.get_agent_map", return_value={}),
            patch("router.approvals.execute._run_in_container", new_callable=AsyncMock) as mock_run,
        ):
            await app_module._execute_approved_draft(draft, "C001", "1.0", client)

        mock_run.assert_not_called()
        client.chat_postMessage.assert_called_once()
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "unknown agent" in text

    @pytest.mark.asyncio
    async def test_execute_approved_draft_dispatch_non_json_stdout_posts_to_slack(self, app_module):
        """When docker exec returns non-JSON stdout, post a Slack error without
        crashing. Guards against handler startup failures (import errors, etc.)."""
        draft = self._make_draft(
            "dispatch",
            "dispatch_issue",
            {"issue_url": "https://github.com/org/repo/issues/3"},
        )
        client = AsyncMock()

        with (
            patch("router.approvals.execute._workers_client", return_value=None),
            patch(
                "router.approvals.execute.get_agent_map",
                return_value={"lisa": {"container": "lisa-container", "name": "Lisa"}},
            ),
            patch(
                "router.approvals.execute._run_in_container",
                new_callable=AsyncMock,
                return_value=("Traceback (most recent call last):\n  ImportError: ...", "", 1),
            ),
        ):
            await app_module._execute_approved_draft(draft, "C001", "1.0", client)

        client.chat_postMessage.assert_called_once()
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "non-JSON" in text

    @pytest.mark.asyncio
    async def test_lifecycle_ack_posts_via_workers_client_when_token_set(self, app_module, monkeypatch):
        """Issue #270: the launched ack reports *on a dispatch*, so it speaks as
        the workers bot — a client built with ``WORKERS_BOT_TOKEN`` — not the
        agent ``client`` whose bolt app handled the approval click."""
        import json as _json

        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-workers-ack")

        workers_client = MagicMock()
        workers_client.token = "xoxb-workers-ack"
        workers_client.chat_postMessage = AsyncMock(return_value={"ok": True})
        ctor = MagicMock(return_value=workers_client)

        draft = self._make_draft(
            "dispatch",
            "dispatch_issue",
            {"issue_url": "https://github.com/org/repo/issues/270"},
        )
        client = AsyncMock()

        run_result = (_json.dumps({"status": "launched", "dispatch_id": "dispatch-w270"}), "", 0)
        with (
            patch("router.runtime.AsyncWebClient", ctor),
            patch(
                "router.approvals.execute.get_agent_map",
                return_value={"lisa": {"container": "lisa-container", "name": "Lisa"}},
            ),
            patch(
                "router.approvals.execute._run_in_container",
                new_callable=AsyncMock,
                return_value=run_result,
            ),
        ):
            await app_module._execute_approved_draft(draft, "C001", "1.0", client)

        # Built with the workers token (criterion 1: client.token == WORKERS_BOT_TOKEN).
        ctor.assert_called_once_with(token="xoxb-workers-ack")
        # The launched ack went out through the workers bot, not the agent client.
        workers_client.chat_postMessage.assert_awaited_once()
        text = workers_client.chat_postMessage.call_args.kwargs["text"]
        assert "dispatch-w270" in text
        assert "launched" in text
        client.chat_postMessage.assert_not_called()

    @pytest.mark.asyncio
    async def test_lifecycle_error_ack_posts_via_workers_client(self, app_module, monkeypatch):
        """The ``missing issue_url`` error envelope is a dispatch lifecycle post
        too, so with a workers token it must route through the workers bot."""
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-workers-ack")

        workers_client = MagicMock()
        workers_client.chat_postMessage = AsyncMock(return_value={"ok": True})

        draft = self._make_draft("dispatch", "dispatch_issue", {})  # no issue_url
        client = AsyncMock()

        with patch("router.runtime.AsyncWebClient", MagicMock(return_value=workers_client)):
            await app_module._execute_approved_draft(draft, "C001", "1.0", client)

        workers_client.chat_postMessage.assert_awaited_once()
        text = workers_client.chat_postMessage.call_args.kwargs["text"]
        assert "missing issue_url" in text
        client.chat_postMessage.assert_not_called()

    @pytest.mark.asyncio
    async def test_lifecycle_ack_falls_back_to_agent_client_without_token(
        self, app_module, isolated_settings, monkeypatch
    ):
        """No ``WORKERS_BOT_TOKEN`` and no secret-store entry → the lifecycle ack
        safe-degrades to the agent client, and no workers client is built."""
        import json as _json

        monkeypatch.delenv("WORKERS_BOT_TOKEN", raising=False)
        ctor = MagicMock(side_effect=AssertionError("workers client built without token"))

        draft = self._make_draft(
            "dispatch",
            "dispatch_issue",
            {"issue_url": "https://github.com/org/repo/issues/270"},
        )
        client = AsyncMock()

        run_result = (_json.dumps({"status": "launched", "dispatch_id": "dispatch-fb"}), "", 0)
        with (
            patch("router.runtime.AsyncWebClient", ctor),
            patch(
                "router.approvals.execute.get_agent_map",
                return_value={"lisa": {"container": "lisa-container", "name": "Lisa"}},
            ),
            patch(
                "router.approvals.execute._run_in_container",
                new_callable=AsyncMock,
                return_value=run_result,
            ),
        ):
            await app_module._execute_approved_draft(draft, "C001", "1.0", client)

        ctor.assert_not_called()
        client.chat_postMessage.assert_called_once()
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "dispatch-fb" in text

    @pytest.mark.asyncio
    async def test_lifecycle_ack_reads_workers_token_from_secret_store(
        self, app_module, isolated_settings, monkeypatch
    ):
        """Issue #274: env absent but secret store has token → lifecycle ack posts
        via the workers bot, not the agent client."""
        import json as _json

        monkeypatch.delenv("WORKERS_BOT_TOKEN", raising=False)
        isolated_settings.set_str("workers_bot_token", "xoxb-from-store-274")

        workers_client = MagicMock()
        workers_client.chat_postMessage = AsyncMock(return_value={"ok": True})
        ctor = MagicMock(return_value=workers_client)

        draft = self._make_draft(
            "dispatch",
            "dispatch_issue",
            {"issue_url": "https://github.com/org/repo/issues/274"},
        )
        client = AsyncMock()

        run_result = (_json.dumps({"status": "launched", "dispatch_id": "dispatch-274"}), "", 0)
        with (
            patch("router.runtime.AsyncWebClient", ctor),
            patch(
                "router.approvals.execute.get_agent_map",
                return_value={"lisa": {"container": "lisa-container", "name": "Lisa"}},
            ),
            patch(
                "router.approvals.execute._run_in_container",
                new_callable=AsyncMock,
                return_value=run_result,
            ),
        ):
            await app_module._execute_approved_draft(draft, "C001", "1.0", client)

        ctor.assert_called_once_with(token="xoxb-from-store-274")
        workers_client.chat_postMessage.assert_awaited_once()
        text = workers_client.chat_postMessage.call_args.kwargs["text"]
        assert "dispatch-274" in text
        client.chat_postMessage.assert_not_called()


# ── _system_task_client: dispatch supervision posts as the workers bot (#270) ─


class TestSystemTaskClient:
    """The scheduler routes system (dispatch-supervision) tasks through this
    resolver so their posts speak as the workers bot, not the task owner."""

    def test_prefers_workers_client_when_token_set(self, app_module, monkeypatch):
        monkeypatch.setenv("WORKERS_BOT_TOKEN", "xoxb-workers-sys")

        workers_client = MagicMock(name="workers_client")
        agent_client = MagicMock(name="agent_client")
        with (
            patch("router.runtime.AsyncWebClient", MagicMock(return_value=workers_client)) as ctor,
            patch("router.app._client_for_agent", return_value=agent_client),
        ):
            resolved = app_module._system_task_client("sam")

        ctor.assert_called_once_with(token="xoxb-workers-sys")
        assert resolved is workers_client

    def test_falls_back_to_agent_client_without_token(self, app_module, isolated_settings, monkeypatch):
        monkeypatch.delenv("WORKERS_BOT_TOKEN", raising=False)

        agent_client = MagicMock(name="agent_client")
        ctor = MagicMock(side_effect=AssertionError("workers client built without token"))
        with (
            patch("router.runtime.AsyncWebClient", ctor),
            patch("router.app._client_for_agent", return_value=agent_client),
        ):
            resolved = app_module._system_task_client("sam")

        ctor.assert_not_called()
        assert resolved is agent_client

    def test_reads_workers_token_from_secret_store_when_env_unset(self, app_module, isolated_settings, monkeypatch):
        """Issue #274: env absent → fall through to SecretStore for the workers token."""
        monkeypatch.delenv("WORKERS_BOT_TOKEN", raising=False)
        isolated_settings.set_str("workers_bot_token", "xoxb-from-store")

        workers_client = MagicMock(name="workers_client")
        agent_client = MagicMock(name="agent_client")
        with (
            patch("router.runtime.AsyncWebClient", MagicMock(return_value=workers_client)) as ctor,
            patch("router.app._client_for_agent", return_value=agent_client),
        ):
            resolved = app_module._system_task_client("sam")

        ctor.assert_called_once_with(token="xoxb-from-store")
        assert resolved is workers_client


# ── _is_dispatch_bot_sender / bot-message guard whitelist (#233) ─────


class TestDispatchBotSender:
    """Tests for the identity-based bot-message guard (issue #233).

    The guard allows bot events through only when the sender's user ID (U…)
    appears in the DISPATCH_BOT_USER_IDS allowlist.  The receiving agent's own
    user ID is always blocked regardless of the allowlist, and any free-form
    message text is accepted from whitelisted senders.
    """

    _AGENT = "lisa"
    _OWN_USER_ID = "ULISA_OWN"
    _BOT_USER_ID = "USUPERVISOR"
    _OTHER_USER_ID = "UOTHER_BOT"

    def _make_event(self, user: str, text: str = "review time") -> dict:
        return {
            "bot_id": "B_SOME_BOT",
            "subtype": "bot_message",
            "user": user,
            "text": text,
            "channel": "C001",
            "ts": "1.0",
            "thread_ts": "1.0",
        }

    @pytest.mark.asyncio
    async def test_whitelisted_sender_bypasses_guard(self, app_module):
        """Bot message from a whitelisted user ID must reach dispatch."""
        app_module._bot_user_id_by_agent[self._AGENT] = self._OWN_USER_ID
        app_module._dispatch_bot_user_ids.clear()
        app_module._dispatch_bot_user_ids.update({self._BOT_USER_ID})
        try:
            event = self._make_event(self._BOT_USER_ID, "dispatch review requested")
            say = AsyncMock()
            client = AsyncMock()

            with (
                patch(
                    "router.slack_events.get_agent_map",
                    return_value={self._AGENT: {"container": "lisa", "name": "Lisa"}},
                ),
                patch("router.slack_events.find_session_by_thread", return_value=None),
                patch(
                    "router.slack_events.create_session",
                    return_value={"session_id": "s1", "agent_name": self._AGENT},
                ),
                patch(
                    "router.slack_events.dispatch",
                    new_callable=AsyncMock,
                    return_value={"response": "LGTM"},
                ) as mock_dispatch,
                patch("router.slack_events.update_activity"),
                patch("router.slack_events.add_to_thread_history"),
            ):
                await app_module._handle_event(event, say, client, receiving_agent=self._AGENT, was_mentioned=True)
                mock_dispatch.assert_called_once()
                assert mock_dispatch.call_args.kwargs["agent_name"] == self._AGENT
        finally:
            app_module._bot_user_id_by_agent.pop(self._AGENT, None)
            app_module._dispatch_bot_user_ids.clear()

    @pytest.mark.asyncio
    async def test_own_user_id_allowed_when_whitelisted(self, app_module):
        """Agent's own user ID is allowed when present in the allowlist.

        This is the supervisor self-ping path: the supervisor posts the
        auto-review handoff via the receiving agent's own bolt client, so
        the resulting Slack event arrives with ``user`` = the agent's own
        bot user ID. Startup auto-seeds the allowlist with every resolved
        agent user ID, so this event must bypass the bot-message guard.
        """
        app_module._bot_user_id_by_agent[self._AGENT] = self._OWN_USER_ID
        app_module._dispatch_bot_user_ids.clear()
        app_module._dispatch_bot_user_ids.update({self._OWN_USER_ID})
        try:
            event = self._make_event(self._OWN_USER_ID, "review PR https://example.com/pr/1")
            say = AsyncMock()
            client = AsyncMock()

            with (
                patch(
                    "router.slack_events.get_agent_map",
                    return_value={self._AGENT: {"container": "lisa", "name": "Lisa"}},
                ),
                patch("router.slack_events.find_session_by_thread", return_value=None),
                patch(
                    "router.slack_events.create_session",
                    return_value={"session_id": "s1", "agent_name": self._AGENT},
                ),
                patch(
                    "router.slack_events.dispatch",
                    new_callable=AsyncMock,
                    return_value={"response": "LGTM"},
                ) as mock_dispatch,
                patch("router.slack_events.update_activity"),
                patch("router.slack_events.add_to_thread_history"),
            ):
                await app_module._handle_event(event, say, client, receiving_agent=self._AGENT, was_mentioned=True)
                mock_dispatch.assert_called_once()
        finally:
            app_module._bot_user_id_by_agent.pop(self._AGENT, None)
            app_module._dispatch_bot_user_ids.clear()

    @pytest.mark.asyncio
    async def test_own_user_id_blocked_when_not_whitelisted(self, app_module):
        """Agent's own user ID is blocked when allowlist is empty (no auto-seed).

        Belt-and-braces: without the startup auto-seed having run (e.g. tests
        that don't invoke main()), an event from the agent's own user ID must
        still be dropped — the predicate has no special self-id logic, it
        relies purely on allowlist membership.
        """
        app_module._bot_user_id_by_agent[self._AGENT] = self._OWN_USER_ID
        app_module._dispatch_bot_user_ids.clear()
        try:
            event = self._make_event(self._OWN_USER_ID)
            say = AsyncMock()
            client = AsyncMock()

            await app_module._handle_event(event, say, client, receiving_agent=self._AGENT, was_mentioned=True)
            say.assert_not_called()
        finally:
            app_module._bot_user_id_by_agent.pop(self._AGENT, None)
            app_module._dispatch_bot_user_ids.clear()

    @pytest.mark.asyncio
    async def test_non_whitelisted_bot_blocked(self, app_module):
        """Bot message from a user ID not in the allowlist must be dropped."""
        app_module._bot_user_id_by_agent[self._AGENT] = self._OWN_USER_ID
        app_module._dispatch_bot_user_ids.clear()
        app_module._dispatch_bot_user_ids.update({self._BOT_USER_ID})
        try:
            event = self._make_event(self._OTHER_USER_ID)
            say = AsyncMock()
            client = AsyncMock()

            await app_module._handle_event(event, say, client, receiving_agent=self._AGENT, was_mentioned=True)
            say.assert_not_called()
        finally:
            app_module._bot_user_id_by_agent.pop(self._AGENT, None)
            app_module._dispatch_bot_user_ids.clear()

    @pytest.mark.asyncio
    async def test_empty_allowlist_blocks_all_bots(self, app_module):
        """When DISPATCH_BOT_USER_IDS is empty, all bot messages are dropped."""
        app_module._bot_user_id_by_agent[self._AGENT] = self._OWN_USER_ID
        app_module._dispatch_bot_user_ids.clear()
        try:
            event = self._make_event(self._BOT_USER_ID)
            say = AsyncMock()
            client = AsyncMock()

            await app_module._handle_event(event, say, client, receiving_agent=self._AGENT, was_mentioned=True)
            say.assert_not_called()
        finally:
            app_module._bot_user_id_by_agent.pop(self._AGENT, None)

    @pytest.mark.asyncio
    async def test_free_form_text_accepted_from_whitelisted_sender(self, app_module):
        """Whitelisted sender may use any free-form message text (no regex required)."""
        app_module._bot_user_id_by_agent[self._AGENT] = self._OWN_USER_ID
        app_module._dispatch_bot_user_ids.clear()
        app_module._dispatch_bot_user_ids.update({self._BOT_USER_ID})
        try:
            event = self._make_event(self._BOT_USER_ID, "hey please review PR https://example.com/pr/1 when ready")
            say = AsyncMock()
            client = AsyncMock()

            with (
                patch(
                    "router.slack_events.get_agent_map",
                    return_value={self._AGENT: {"container": "lisa", "name": "Lisa"}},
                ),
                patch("router.slack_events.find_session_by_thread", return_value=None),
                patch(
                    "router.slack_events.create_session",
                    return_value={"session_id": "s1", "agent_name": self._AGENT},
                ),
                patch(
                    "router.slack_events.dispatch",
                    new_callable=AsyncMock,
                    return_value={"response": "ok"},
                ) as mock_dispatch,
                patch("router.slack_events.update_activity"),
                patch("router.slack_events.add_to_thread_history"),
            ):
                await app_module._handle_event(event, say, client, receiving_agent=self._AGENT, was_mentioned=True)
                mock_dispatch.assert_called_once()
        finally:
            app_module._bot_user_id_by_agent.pop(self._AGENT, None)
            app_module._dispatch_bot_user_ids.clear()

    @pytest.mark.asyncio
    async def test_no_user_field_blocked(self, app_module):
        """Bot event with no user field must be dropped even if allowlist is non-empty."""
        app_module._bot_user_id_by_agent[self._AGENT] = self._OWN_USER_ID
        app_module._dispatch_bot_user_ids.clear()
        app_module._dispatch_bot_user_ids.update({self._BOT_USER_ID})
        try:
            event = {
                "bot_id": "B_SOME_BOT",
                "subtype": "bot_message",
                "text": "hello",
                "channel": "C001",
                "ts": "1.0",
            }
            say = AsyncMock()
            client = AsyncMock()

            await app_module._handle_event(event, say, client, receiving_agent=self._AGENT, was_mentioned=True)
            say.assert_not_called()
        finally:
            app_module._bot_user_id_by_agent.pop(self._AGENT, None)
            app_module._dispatch_bot_user_ids.clear()


# ── Worker→agent mention carve-out / WORKER_MENTION_HANDOFF (#283) ───────────


class TestWorkerMentionHandoff:
    """Tests for the WORKER_MENTION_HANDOFF carve-out (issue #283).

    When WORKER_MENTION_HANDOFF=1 and the workers-bot message contains an
    explicit @mention of a persona bot, the message flows past the bot-guard
    to the mention router.  All other workers-bot messages are dropped.
    """

    _WORKERS_BOT_UID = "U0B8SG0GUQN"
    _SAM_BOT_UID = "U_SAM_BOT"
    _HUMAN_UID = "U0AHCJEHVNJ"
    _AGENT = "sam"

    def _make_event(self, text: str = "") -> dict:
        """Return a bot-authored message event shaped as Bolt delivers it."""
        return {
            "bot_id": "B_WORKERS",
            "subtype": "bot_message",
            "user": self._WORKERS_BOT_UID,
            "text": text,
            "channel": "C001",
            "ts": "1.0",
            "thread_ts": "1.0",
            "type": "message",
        }

    def _setup(self, app_module):
        router_runtime.workers_bot_user_id = self._WORKERS_BOT_UID
        app_module._bot_user_id_by_agent[self._AGENT] = self._SAM_BOT_UID
        app_module._dispatch_bot_user_ids.clear()
        app_module._dispatch_bot_user_ids.update({self._WORKERS_BOT_UID, self._SAM_BOT_UID})

    def _teardown(self, app_module):
        router_runtime.workers_bot_user_id = None
        app_module._bot_user_id_by_agent.pop(self._AGENT, None)
        app_module._dispatch_bot_user_ids.clear()

    # ── AC1 ──────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_flag_on_persona_mention_wakes_agent(self, app_module, monkeypatch):
        """AC1: workers-bot + persona mention + flag=1 reaches the mention router."""
        self._setup(app_module)
        monkeypatch.setenv("WORKER_MENTION_HANDOFF", "1")
        try:
            event = self._make_event(f"PR ready, <@{self._SAM_BOT_UID}> please review")
            say = AsyncMock()
            client = AsyncMock()

            with (
                patch(
                    "router.slack_events.get_agent_map",
                    return_value={self._AGENT: {"container": "sam", "name": "Sam"}},
                ),
                patch("router.slack_events.find_session_by_thread", return_value=None),
                patch(
                    "router.slack_events.create_session",
                    return_value={"session_id": "s1", "agent_name": self._AGENT},
                ),
                patch(
                    "router.slack_events.dispatch",
                    new_callable=AsyncMock,
                    return_value={"response": "ok"},
                ) as mock_dispatch,
                patch("router.slack_events.update_activity"),
                patch("router.slack_events.add_to_thread_history"),
            ):
                await app_module._handle_event(event, say, client, receiving_agent=self._AGENT, was_mentioned=True)
                mock_dispatch.assert_called_once()
                assert mock_dispatch.call_args.kwargs["agent_name"] == self._AGENT
        finally:
            self._teardown(app_module)

    # ── AC2 ──────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_flag_on_no_mention_still_dropped(self, app_module, monkeypatch):
        """AC2: workers-bot + no persona mention + flag=1 is still dropped."""
        self._setup(app_module)
        monkeypatch.setenv("WORKER_MENTION_HANDOFF", "1")
        try:
            event = self._make_event("Worker job done, no mention here")
            say = AsyncMock()
            client = AsyncMock()

            await app_module._handle_event(event, say, client, receiving_agent=self._AGENT, was_mentioned=True)
            say.assert_not_called()
        finally:
            self._teardown(app_module)

    # ── AC3 ──────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_flag_on_non_persona_mention_still_dropped(self, app_module, monkeypatch):
        """AC3: workers-bot + non-persona @mention + flag=1 is still dropped."""
        self._setup(app_module)
        monkeypatch.setenv("WORKER_MENTION_HANDOFF", "1")
        try:
            event = self._make_event(f"cc <@{self._HUMAN_UID}>")
            say = AsyncMock()
            client = AsyncMock()

            await app_module._handle_event(event, say, client, receiving_agent=self._AGENT, was_mentioned=True)
            say.assert_not_called()
        finally:
            self._teardown(app_module)

    # ── AC4 ──────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_flag_off_workers_bot_always_dropped(self, app_module, monkeypatch):
        """AC4: flag=0 (default) — workers-bot dropped regardless of mention."""
        self._setup(app_module)
        monkeypatch.setenv("WORKER_MENTION_HANDOFF", "0")
        try:
            event = self._make_event(f"PR ready, <@{self._SAM_BOT_UID}> please review")
            say = AsyncMock()
            client = AsyncMock()

            await app_module._handle_event(event, say, client, receiving_agent=self._AGENT, was_mentioned=True)
            say.assert_not_called()
        finally:
            self._teardown(app_module)

    @pytest.mark.asyncio
    async def test_flag_absent_workers_bot_always_dropped(self, app_module, monkeypatch):
        """AC4 (env absent): no WORKER_MENTION_HANDOFF env var — drop is the default."""
        self._setup(app_module)
        monkeypatch.delenv("WORKER_MENTION_HANDOFF", raising=False)
        try:
            event = self._make_event(f"PR ready, <@{self._SAM_BOT_UID}> please review")
            say = AsyncMock()
            client = AsyncMock()

            await app_module._handle_event(event, say, client, receiving_agent=self._AGENT, was_mentioned=True)
            say.assert_not_called()
        finally:
            self._teardown(app_module)

    # ── AC5 ──────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_persona_bot_self_post_not_widened_by_carve_out(self, app_module, monkeypatch):
        """AC5: carve-out does not open a self-event path for persona bots.

        The carve-out only triggers when the sender is the workers-bot user ID.
        A message arriving with the agent's own bot UID as sender (the self-
        mention path) is unaffected — it reaches _is_dispatch_bot_sender via
        the existing allowlist path, not the workers-bot branch, so behaviour
        is unchanged from before this PR.
        """
        self._setup(app_module)
        monkeypatch.setenv("WORKER_MENTION_HANDOFF", "1")
        try:
            # Sender is sam-bot (own persona), not workers-bot.
            event = {
                "bot_id": "B_SAM",
                "subtype": "bot_message",
                "user": self._SAM_BOT_UID,
                "text": f"<@{self._SAM_BOT_UID}> self mention",
                "channel": "C001",
                "ts": "2.0",
                "thread_ts": "1.0",
                "type": "message",
            }
            say = AsyncMock()
            client = AsyncMock()

            with (
                patch(
                    "router.slack_events.get_agent_map",
                    return_value={self._AGENT: {"container": "sam", "name": "Sam"}},
                ),
                patch("router.slack_events.find_session_by_thread", return_value=None),
                patch(
                    "router.slack_events.create_session",
                    return_value={"session_id": "s1", "agent_name": self._AGENT},
                ),
                patch(
                    "router.slack_events.dispatch",
                    new_callable=AsyncMock,
                    return_value={"response": "ok"},
                ) as mock_dispatch,
                patch("router.slack_events.update_activity"),
                patch("router.slack_events.add_to_thread_history"),
            ):
                await app_module._handle_event(event, say, client, receiving_agent=self._AGENT, was_mentioned=True)
                # sam-bot is in _dispatch_bot_user_ids (not workers-bot path),
                # so the existing allowlist pass-through applies.
                mock_dispatch.assert_called_once()
        finally:
            self._teardown(app_module)


# ── attachment ingest failure (#425) ────────────────────────────────


class TestAttachmentIngestFailure:
    """Ingest failure must notify the user and abort dispatch (issue #425)."""

    @pytest.mark.asyncio
    async def test_ingest_exception_posts_notice_and_aborts(self, app_module):
        """When ingest_files raises, handler posts a :no_entry: reply and returns
        without calling dispatch."""
        event = {
            "text": "please analyse this",
            "channel": "C001",
            "user": "U001",
            "ts": "1.0",
            "thread_ts": "1.0",
            "files": [
                {
                    "id": "F1",
                    "name": "report.pdf",
                    "mimetype": "application/pdf",
                    "size": 1024,
                    "url_private": "https://files.slack.com/report.pdf",
                }
            ],
        }
        say = AsyncMock()
        client = AsyncMock()
        client.chat_postMessage = AsyncMock()

        with (
            patch("router.slack_events.attachments_enabled", return_value=True),
            patch("router.slack_events.find_session_by_thread", return_value=None),
            patch(
                "router.slack_events.create_session",
                return_value={"session_id": "s1", "agent_name": "lisa"},
            ),
            patch("router.slack_events.config", {"slack_credentials": {"lisa": {"bot_token": "xoxb-test"}}}),
            patch("router.slack_events.validate_files", return_value=([event["files"][0]], None)),
            patch(
                "router.slack_events.ingest_files",
                new_callable=AsyncMock,
                side_effect=RuntimeError("disk full"),
            ),
            patch(
                "router.slack_events.dispatch", new_callable=AsyncMock, return_value={"response": "ok"}
            ) as mock_dispatch,
            patch("router.slack_events.update_activity"),
            patch("router.slack_events.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)

        # A :no_entry: notice must have been posted to the thread
        client.chat_postMessage.assert_called_once()
        call_kwargs = client.chat_postMessage.call_args.kwargs
        assert call_kwargs["channel"] == "C001"
        assert call_kwargs["thread_ts"] == "1.0"
        assert ":no_entry:" in call_kwargs["text"]

        # Dispatch must NOT have been called — the handler aborted
        mock_dispatch.assert_not_called()


# ── session_timeout config robustness (#462) ─────────────────────────


class TestSessionTimeoutConfigFallback:
    """A config dict missing ``session_timeout`` must not crash _handle_event.

    Regression for PR #528: the #462 fix introduced a hard
    ``config["session_timeout"]`` subscript in the active-thread session
    lookup, which raised KeyError for any code path/test passing a partial
    config. All session-timeout reads now use ``config.get(...)`` and every
    consumer falls back to ``DEFAULT_TIMEOUT_SECONDS`` (600) on ``None``.
    """

    @pytest.mark.asyncio
    async def test_missing_session_timeout_key_falls_back_to_none(self, app_module):
        """When config lacks ``session_timeout``, the handler dispatches without
        raising and forwards ``timeout_seconds=None`` to the session lookup."""
        event = {
            "text": "hello Lisa",
            "channel": "C001",
            "user": "U001",
            "ts": "1.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()
        client.reactions_add = AsyncMock()

        find_session = MagicMock(return_value=None)
        with (
            # config WITHOUT a "session_timeout" key — the regression trigger.
            patch("router.slack_events.config", {"slack_credentials": {"lisa": {"bot_token": "xoxb-test"}}}),
            patch("router.slack_events.find_session_by_thread", find_session),
            patch("router.slack_events.create_session", return_value={"session_id": "s1", "agent_name": "lisa"}),
            patch(
                "router.slack_events.dispatch", new_callable=AsyncMock, return_value={"response": "ok"}
            ) as mock_dispatch,
            patch("router.slack_events.update_activity"),
            patch("router.slack_events.add_to_thread_history"),
        ):
            # Must not raise KeyError.
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)

        find_session.assert_called_once()
        assert find_session.call_args.kwargs["timeout_seconds"] is None
        assert mock_dispatch.call_args.kwargs["timeout"] is None


class TestGuard1PeerSummaryDispatch:
    """Guard 1 (issue #547): peer/harness summary messages must not create dispatchable turns.

    A peer bot posting a session summary into a thread owned by agent B must be
    silently skipped — B is never invoked to reply to it. Normal messages from the
    same whitelisted sender still produce turns (happy-path regression guard).
    """

    @pytest.mark.asyncio
    async def test_peer_summary_does_not_dispatch(self, app_module):
        """A whitelisted dispatch-bot posting a session summary must NOT dispatch to the agent."""
        peer_bot_user = "U_LIN_BOT"

        app_module._dispatch_bot_user_ids.add(peer_bot_user)
        try:
            event = {
                "bot_id": "B_LIN",
                "user": peer_bot_user,
                "text": "## Session Summary\nCompleted billing module. Key decisions: …",
                "channel": "C001",
                "ts": "1.0",
                "thread_ts": "1.0",
            }
            say = AsyncMock()
            client = AsyncMock()

            with patch("router.slack_events.dispatch", new_callable=AsyncMock) as mock_dispatch:
                await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=False)
                mock_dispatch.assert_not_called()
                say.assert_not_called()
        finally:
            app_module._dispatch_bot_user_ids.discard(peer_bot_user)

    @pytest.mark.asyncio
    async def test_peer_session_paused_summary_does_not_dispatch(self, app_module):
        """A peer _Session paused… message must NOT dispatch to the agent."""
        peer_bot_user = "U_LIN_BOT2"

        app_module._dispatch_bot_user_ids.add(peer_bot_user)
        try:
            event = {
                "bot_id": "B_LIN",
                "user": peer_bot_user,
                "text": "_Session paused. Here's where we left off:_\n_Topic: billing_",
                "channel": "C001",
                "ts": "2.0",
                "thread_ts": "1.0",
            }
            say = AsyncMock()
            client = AsyncMock()

            with patch("router.slack_events.dispatch", new_callable=AsyncMock) as mock_dispatch:
                await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=False)
                mock_dispatch.assert_not_called()
        finally:
            app_module._dispatch_bot_user_ids.discard(peer_bot_user)

    @pytest.mark.asyncio
    async def test_ordinary_bot_message_still_dispatches(self, app_module):
        """A normal (non-summary) message from a whitelisted bot STILL creates a turn."""
        peer_bot_user = "U_LIN_BOT3"

        app_module._dispatch_bot_user_ids.add(peer_bot_user)
        try:
            event = {
                "bot_id": "B_LIN",
                "user": peer_bot_user,
                "text": "Hey Lisa, can you review the auth PR?",
                "channel": "C001",
                "ts": "3.0",
                "thread_ts": "1.0",
            }
            say = AsyncMock()
            client = AsyncMock()

            with (
                patch("router.slack_events.find_session_by_thread", return_value=None),
                patch("router.slack_events.create_session", return_value={"session_id": "s1"}),
                patch(
                    "router.slack_events.dispatch",
                    new_callable=AsyncMock,
                    return_value={"response": "On it!"},
                ) as mock_dispatch,
                patch("router.slack_events.update_activity"),
                patch("router.slack_events.add_to_thread_history"),
            ):
                await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=False)
                mock_dispatch.assert_called_once()
        finally:
            app_module._dispatch_bot_user_ids.discard(peer_bot_user)


# ── DISCORD_ENABLED gate ──────────────────────────────────────────────────────


class TestDiscordEnabledGate:
    """Tests for the DISCORD_ENABLED feature gate in _build_discord_adapters."""

    def test_gate_off_returns_empty(self, monkeypatch, app_module):
        """DISCORD_ENABLED unset → no adapters built."""
        monkeypatch.delenv("DISCORD_ENABLED", raising=False)
        result = app_module._build_discord_adapters()
        assert result == []

    def test_gate_false_returns_empty(self, monkeypatch, app_module):
        """DISCORD_ENABLED=false → no adapters built."""
        monkeypatch.setenv("DISCORD_ENABLED", "false")
        result = app_module._build_discord_adapters()
        assert result == []

    def test_gate_zero_returns_empty(self, monkeypatch, app_module):
        """DISCORD_ENABLED=0 → no adapters built."""
        monkeypatch.setenv("DISCORD_ENABLED", "0")
        result = app_module._build_discord_adapters()
        assert result == []

    def test_gate_off_slack_apps_unchanged(self, monkeypatch, app_module):
        """When DISCORD_ENABLED is off, the Slack _apps_by_agent dict is not modified."""
        monkeypatch.delenv("DISCORD_ENABLED", raising=False)
        slack_agents_before = set(app_module._apps_by_agent.keys())
        app_module._build_discord_adapters()
        assert set(app_module._apps_by_agent.keys()) == slack_agents_before

    def test_gate_on_no_creds_returns_empty(self, monkeypatch, app_module):
        """DISCORD_ENABLED=true but no Discord tokens → no adapters, no error."""
        monkeypatch.setenv("DISCORD_ENABLED", "true")

        with patch("router.app.load_discord_credentials", return_value={}):
            result = app_module._build_discord_adapters()
        assert result == []

    def test_gate_on_builds_one_adapter_per_agent(self, monkeypatch, app_module):
        """DISCORD_ENABLED=true + credentials → one DiscordAdapter per agent."""
        monkeypatch.setenv("DISCORD_ENABLED", "true")
        mock_creds = {
            "sam": {"bot_token": "discord-token-sam", "default_channel_id": 0},
            "lisa": {"bot_token": "discord-token-lisa", "default_channel_id": 0},
        }
        mock_adapter = MagicMock()
        with (
            patch("router.app.load_discord_credentials", return_value=mock_creds),
            patch("router.chat.adapters.discord.DiscordAdapter", return_value=mock_adapter),
        ):
            result = app_module._build_discord_adapters()
        assert len(result) == 2

    def test_gate_on_registers_approval_interaction_handler(self, monkeypatch, app_module):
        """#682: each built adapter must get the approval interaction handler
        registered — without this a button click has nothing listening for
        it and the approve→execute loop stays dead on Discord."""
        monkeypatch.setenv("DISCORD_ENABLED", "true")
        mock_creds = {"sam": {"bot_token": "discord-token-sam", "default_channel_id": 0}}
        mock_adapter = MagicMock()
        with (
            patch("router.app.load_discord_credentials", return_value=mock_creds),
            patch("router.chat.adapters.discord.DiscordAdapter", return_value=mock_adapter),
        ):
            app_module._build_discord_adapters()

        mock_adapter.register_interaction_handler.assert_called_once()
        from router.approvals.discord_handlers import _on_interaction

        assert mock_adapter.register_interaction_handler.call_args.args[0] is _on_interaction


# ── _execute_approved_discord_draft ─────────────────────────────────────────


class TestExecuteApprovedDiscordDraft:
    """Tests for the Discord approval-button bridge callback (#682)."""

    def _make_draft(self, **overrides):
        from router.approvals.store import Draft

        defaults = {
            "draft_id": "test-discord-draft-001",
            "agent_name": "sam",
            "capability_type": "pack",
            "capability_instance": "dispatch",
            "action_verb": "dispatch_issue",
            "payload": {
                "issue_url": "https://github.com/org/repo/issues/682",
                "transport": "discord",
                "conversation_id": "discord:111:222:333",
            },
            "slack_channel": "discord:111:222:333",
            "slack_message_ts": "999",
        }
        defaults.update(overrides)
        return Draft(**defaults)

    @pytest.mark.asyncio
    async def test_wraps_matching_adapter_in_session_client_facade(self, app_module):
        draft = self._make_draft()
        adapter = MagicMock()
        adapter.agent_name = "sam"
        router_runtime.discord_adapters.append(adapter)

        with patch.object(app_module, "_execute_approved_draft", new_callable=AsyncMock) as mock_execute:
            await app_module._execute_approved_discord_draft(draft, interaction=MagicMock())

        mock_execute.assert_awaited_once()
        called_draft, called_channel, called_thread, called_client = mock_execute.call_args.args
        assert called_draft.draft_id == draft.draft_id
        assert called_channel == ""
        assert called_thread == ""
        assert called_client._adapter is adapter
        assert called_client._ref == "discord:111:222:333"

    @pytest.mark.asyncio
    async def test_no_matching_adapter_is_noop(self, app_module):
        draft = self._make_draft()
        # runtime.discord_adapters is empty (cleared by the app_module fixture).

        with patch.object(app_module, "_execute_approved_draft", new_callable=AsyncMock) as mock_execute:
            await app_module._execute_approved_discord_draft(draft, interaction=MagicMock())

        mock_execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_conversation_id_is_noop(self, app_module):
        draft = self._make_draft(payload={"issue_url": "https://github.com/org/repo/issues/682"})
        adapter = MagicMock()
        adapter.agent_name = "sam"
        router_runtime.discord_adapters.append(adapter)

        with patch.object(app_module, "_execute_approved_draft", new_callable=AsyncMock) as mock_execute:
            await app_module._execute_approved_discord_draft(draft, interaction=MagicMock())

        mock_execute.assert_not_awaited()
