"""Unit tests for router.app — Slack event handling and session management.

Tests mock all external dependencies (Slack API, dispatcher, session manager)
so no Slack connection or Docker daemon is needed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# We need to patch module-level side effects before importing app.
# app.py calls load_dotenv(), load_config(), and creates an AsyncApp at import time.


@pytest.fixture()
def app_module(monkeypatch, tmp_path):
    """Import router.app with all module-level side effects mocked."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-secret")

    with (
        patch("router.app.AsyncApp") as mock_app_cls,
        patch("router.app.load_dotenv"),
    ):
        mock_bolt_app = MagicMock()
        mock_bolt_app.client = MagicMock()
        mock_app_cls.return_value = mock_bolt_app

        import importlib  # noqa: E402

        import router.app  # noqa: E402
        import router.threads.state as thread_state_mod  # noqa: E402

        importlib.reload(router.app)

        # Patch after reload so the module-level names are overridden
        monkeypatch.setattr(router.app, "needs_curation", lambda *a, **kw: False)
        monkeypatch.setattr(router.app, "curate_agent_memory", AsyncMock())

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


# ── _resolve_agent ──────────────────────────────────────────────────


class TestResolveAgent:
    """Tests for the _resolve_agent helper."""

    def test_resolve_defaults_to_lisa(self, app_module):
        """When no bot user is mentioned, default to lisa (not mentioned)."""
        event = {"text": "Hello there"}
        assert app_module._resolve_agent(event) == ("lisa", False)

    def test_resolve_matches_bot_user_map(self, app_module):
        """When a bot user ID is mentioned, resolve to that agent (mentioned)."""
        app_module._bot_user_map["U_BOT_LISA"] = "lisa"
        event = {"text": "Hey <@U_BOT_LISA> can you help?"}
        agent, mentioned = app_module._resolve_agent(event)
        assert agent == "lisa"
        assert mentioned is True
        # Cleanup
        app_module._bot_user_map.clear()

    def test_resolve_no_text(self, app_module):
        """Event without text should default to lisa."""
        event = {}
        assert app_module._resolve_agent(event) == ("lisa", False)

    def test_resolve_plain_name_mention(self, app_module):
        """A plain `@lisa` name mention should also resolve."""
        event = {"text": "Can @lisa look at this?"}
        agent, mentioned = app_module._resolve_agent(event)
        assert agent == "lisa"
        assert mentioned is True

    def test_resolve_uses_thread_active_agent(self, app_module):
        """When no mention, _resolve_agent should consult thread state."""
        from router.threads.state import get_default_store

        # Register a second agent so we can verify the thread-state lookup
        # selects it over the default.
        with patch(
            "router.app.get_agent_map",
            return_value={
                "lisa": {"container": "lisa", "name": "Lisa"},
                "sam": {"container": "sam", "name": "Sam"},
            },
        ):
            get_default_store().set_active_agent("C001", "1.0", "sam", mentioned=True)
            event = {"text": "no mention here", "channel": "C001", "thread_ts": "1.0"}
            agent, mentioned = app_module._resolve_agent(event)
        assert agent == "sam"
        assert mentioned is False


# ── _handle_event ───────────────────────────────────────────────────


class TestHandleEvent:
    """Tests for the main event handler."""

    @pytest.mark.asyncio
    async def test_ignores_bot_messages(self, app_module):
        """Events from bots should be ignored to prevent loops."""
        event = {"bot_id": "B001", "text": "bot message", "channel": "C001", "ts": "1.0"}
        say = AsyncMock()
        client = AsyncMock()
        await app_module._handle_event(event, say, client)
        say.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_bot_subtype(self, app_module):
        """Events with subtype bot_message should be ignored."""
        event = {"subtype": "bot_message", "text": "bot msg", "channel": "C001", "ts": "1.0"}
        say = AsyncMock()
        client = AsyncMock()
        await app_module._handle_event(event, say, client)
        say.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_agent_returns_early(self, app_module):
        """If _resolve_agent returns None, handler should return early."""
        event = {"text": "hello", "channel": "C001", "user": "U001", "ts": "1.0"}
        say = AsyncMock()
        client = AsyncMock()
        with patch.object(app_module, "_resolve_agent", return_value=None):
            await app_module._handle_event(event, say, client)
        say.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_not_in_map_returns_early(self, app_module):
        """If agent name is not in agent map, handler should return early."""
        event = {"text": "hello", "channel": "C001", "user": "U001", "ts": "1.0"}
        say = AsyncMock()
        client = AsyncMock()
        with patch.object(app_module, "_resolve_agent", return_value="nonexistent"):
            await app_module._handle_event(event, say, client)
        say.assert_not_called()

    @pytest.mark.asyncio
    async def test_exit_trigger_calls_handle_clean_exit(self, app_module):
        """Exit trigger phrase should invoke handle_clean_exit."""
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
            patch("router.app.find_session_by_thread", return_value={"session_id": "s1", "agent_name": "lisa"}),
            patch("router.app.update_activity"),
            patch("router.app.handle_clean_exit", new_callable=AsyncMock) as mock_exit,
        ):
            await app_module._handle_event(event, say, client)
            mock_exit.assert_called_once()
            say.assert_called_once()
            assert "welcome" in say.call_args[1]["text"].lower() or "saved" in say.call_args[1]["text"].lower()

    @pytest.mark.asyncio
    async def test_exit_trigger_handles_exception(self, app_module):
        """If handle_clean_exit raises, the handler should still say goodbye."""
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
            patch("router.app.find_session_by_thread", return_value={"session_id": "s1", "agent_name": "lisa"}),
            patch("router.app.update_activity"),
            patch("router.app.handle_clean_exit", new_callable=AsyncMock, side_effect=Exception("boom")),
        ):
            await app_module._handle_event(event, say, client)
            say.assert_called_once()

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
            patch("router.app.find_session_by_thread", return_value=None),
            patch("router.app.create_session", return_value={"session_id": "s1"}),
            patch("router.app.dispatch", new_callable=AsyncMock, return_value={"response": "Hi there!"}),
            patch("router.app.update_activity"),
            patch("router.app.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client)
            say.assert_called_once()
            assert say.call_args[1]["text"] == "Hi there!"

    @pytest.mark.asyncio
    async def test_dispatch_error_sends_apology(self, app_module):
        """If dispatch raises, handler should apologize."""
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
            patch("router.app.find_session_by_thread", return_value=None),
            patch("router.app.create_session", return_value={"session_id": "s1"}),
            patch("router.app.dispatch", new_callable=AsyncMock, side_effect=Exception("dispatch failed")),
            patch("router.app.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client)
            say.assert_called_once()
            assert "sorry" in say.call_args[1]["text"].lower() or "wrong" in say.call_args[1]["text"].lower()

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
                "router.app.find_session_by_thread",
                return_value={"session_id": "s1", "agent_name": "lisa"},
            ),
            patch("router.app.update_activity") as mock_update,
            patch("router.app.dispatch", new_callable=AsyncMock, return_value={"response": "Hi!"}),
            patch("router.app.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client)
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
            patch("router.app.find_session_by_thread", return_value=None),
            patch("router.app.create_session", return_value={"session_id": "s1"}),
            patch("router.app.dispatch", new_callable=AsyncMock, return_value={"response": "Hi!"}),
            patch("router.app.update_activity"),
            patch("router.app.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client)
            say.assert_called_once()
            assert say.call_args[1]["text"] == "Hi!"


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
            patch("router.app.find_session_by_thread", return_value=None),
            patch("router.app.create_session", return_value={"session_id": "s1"}),
            patch("router.app.dispatch", new_callable=AsyncMock, return_value={"response": "Hi!"}),
            patch("router.app.update_activity"),
            patch("router.app.add_to_thread_history"),
        ):
            await app_module.handle_message(event, say, client)
            say.assert_called_once()

    @pytest.mark.asyncio
    async def test_channel_thread_reply_with_active_session(self, app_module):
        """Thread reply in a channel with active session should be handled."""
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
                "router.app.find_session_by_thread",
                return_value={"session_id": "s1", "agent_name": "lisa"},
            ),
            patch("router.app.update_activity"),
            patch("router.app.dispatch", new_callable=AsyncMock, return_value={"response": "Reply!"}),
            patch("router.app.add_to_thread_history"),
        ):
            await app_module.handle_message(event, say, client)
            say.assert_called_once()

    @pytest.mark.asyncio
    async def test_channel_thread_reply_no_session_ignored(self, app_module):
        """Thread reply in a channel without active session should be ignored."""
        event = {
            "channel_type": "channel",
            "text": "random reply",
            "channel": "C001",
            "user": "U001",
            "ts": "2.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()

        with patch("router.app.find_session_by_thread", return_value=None):
            await app_module.handle_message(event, say, client)
            say.assert_not_called()

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

        await app_module.handle_message(event, say, client)
        say.assert_not_called()


# ── agent handoff ───────────────────────────────────────────────────


class TestAgentHandoff:
    """Tests for mention-driven multi-agent handoffs."""

    @pytest.mark.asyncio
    async def test_explicit_mention_dispatches_to_mentioned_agent(self, app_module):
        """An explicit @mention routes to the mentioned agent regardless of
        the thread's previously active agent."""
        from router.threads.state import get_default_store

        get_default_store().set_active_agent("C001", "1.0", "lisa", mentioned=True)

        event = {
            "text": "@sam can you weigh in?",
            "channel": "C001",
            "user": "U001",
            "ts": "2.0",
            "thread_ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()

        with (
            patch(
                "router.app.get_agent_map",
                return_value={
                    "lisa": {"container": "lisa", "name": "Lisa"},
                    "sam": {"container": "sam", "name": "Sam"},
                },
            ),
            patch("router.app.find_session_by_thread", return_value=None),
            patch(
                "router.app.create_session",
                return_value={"session_id": "s1", "agent_name": "sam"},
            ),
            patch(
                "router.app.dispatch",
                new_callable=AsyncMock,
                return_value={"response": "Sam here, I can help."},
            ) as mock_dispatch,
            patch("router.app.update_activity"),
            patch("router.app.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client)
            mock_dispatch.assert_called_once()
            assert mock_dispatch.call_args.kwargs["agent_name"] == "sam"

        # Thread state was updated to sam.
        assert get_default_store().get_active_agent("C001", "1.0") == "sam"

    @pytest.mark.asyncio
    async def test_unmentioned_reply_goes_to_active_agent(self, app_module):
        """An un-mentioned reply in a thread should route to the thread's
        active agent (set by a prior mention), not to the default."""
        from router.threads.state import get_default_store

        get_default_store().set_active_agent("C001", "1.0", "sam", mentioned=True)

        event = {
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
                "router.app.get_agent_map",
                return_value={
                    "lisa": {"container": "lisa", "name": "Lisa"},
                    "sam": {"container": "sam", "name": "Sam"},
                },
            ),
            patch("router.app.find_session_by_thread", return_value=None),
            patch(
                "router.app.create_session",
                return_value={"session_id": "s1", "agent_name": "sam"},
            ),
            patch(
                "router.app.dispatch",
                new_callable=AsyncMock,
                return_value={"response": "Got it."},
            ) as mock_dispatch,
            patch("router.app.update_activity"),
            patch("router.app.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client)
            mock_dispatch.assert_called_once()
            assert mock_dispatch.call_args.kwargs["agent_name"] == "sam"

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
                "router.app.get_agent_map",
                return_value={
                    "lisa": {"container": "lisa", "name": "Lisa"},
                    "dave": {"container": "dave", "name": "Dave"},
                },
            ),
            patch("router.app.find_session_by_thread", return_value=None),
            patch(
                "router.app.create_session",
                return_value={"session_id": "s1", "agent_name": "lisa"},
            ),
            patch(
                "router.app.dispatch",
                new_callable=AsyncMock,
                return_value={"response": "I'll loop in @dave on this."},
            ),
            patch("router.app.update_activity"),
            patch("router.app.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client)

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
                "router.app.get_agent_map",
                return_value={"lisa": {"container": "lisa", "name": "Lisa"}},
            ),
            patch("router.app.find_session_by_thread", return_value=None),
            patch(
                "router.app.create_session",
                return_value={"session_id": "s1", "agent_name": "lisa"},
            ),
            patch(
                "router.app.dispatch",
                new_callable=AsyncMock,
                return_value={"response": "Hi from @lisa again!"},
            ),
            patch("router.app.update_activity"),
            patch("router.app.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client)

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
                patch("router.app.find_session_by_thread", return_value=None),
                patch(
                    "router.app.create_session",
                    return_value={"session_id": "s1", "agent_name": "lisa"},
                ),
                patch(
                    "router.app.dispatch",
                    new_callable=AsyncMock,
                    return_value={"response": "ok"},
                ) as mock_dispatch,
                patch("router.app.update_activity"),
                patch("router.app.add_to_thread_history"),
            ):
                await app_module._handle_event(event, say, client)
                kwargs = mock_dispatch.call_args.kwargs
                assert kwargs["bot_user_map"] == {"U_BOT_LISA": "lisa"}
        finally:
            app_module._bot_user_map.clear()


# ── handle_app_mention ──────────────────────────────────────────────


class TestHandleAppMention:
    """Tests for the app_mention event handler."""

    @pytest.mark.asyncio
    async def test_app_mention_delegates_to_handle_event(self, app_module):
        """handle_app_mention should delegate to _handle_event."""
        event = {
            "text": "<@UBOT> help",
            "channel": "C001",
            "user": "U001",
            "ts": "1.0",
        }
        say = AsyncMock()
        client = AsyncMock()

        with patch.object(app_module, "_handle_event", new_callable=AsyncMock) as mock_handler:
            await app_module.handle_app_mention(event, say, client)
            mock_handler.assert_called_once_with(event, say, client)


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

        call_count = 0

        async def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise KeyboardInterrupt("break loop")

        with (
            patch("router.app.asyncio.sleep", side_effect=mock_sleep),
            patch("router.app.pop_timed_out_sessions", return_value=[expired_session]),
            patch("router.app.handle_timeout_exit", new_callable=AsyncMock) as mock_timeout_exit,
            patch("router.app.get_agent_map", return_value={"lisa": {"container": "lisa", "name": "Lisa"}}),
        ):
            with pytest.raises(KeyboardInterrupt):
                await app_module._session_cleanup_loop(interval_seconds=1)

            mock_timeout_exit.assert_called_once()

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
            patch("router.app.pop_timed_out_sessions", return_value=[expired_session]),
            patch("router.app.handle_timeout_exit", new_callable=AsyncMock) as mock_exit,
            patch("router.app.get_agent_map", return_value={"lisa": {"container": "lisa"}}),
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

        call_count = 0

        async def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise KeyboardInterrupt("break loop")

        with (
            patch("router.app.asyncio.sleep", side_effect=mock_sleep),
            patch("router.app.pop_timed_out_sessions", return_value=[expired_session]),
            patch(
                "router.app.handle_timeout_exit",
                new_callable=AsyncMock,
                side_effect=Exception("exit error"),
            ),
            patch("router.app.get_agent_map", return_value={"lisa": {"container": "lisa"}}),
        ):
            with pytest.raises(KeyboardInterrupt):
                await app_module._session_cleanup_loop(interval_seconds=1)

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
            patch("router.app.pop_timed_out_sessions", side_effect=Exception("db error")),
            patch("router.app.get_agent_map", return_value={"lisa": {"container": "lisa"}}),
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
            patch("router.app.pop_timed_out_sessions", return_value=[]),
            patch("router.app.get_agent_map", return_value={"lisa": {"container": "lisa"}}),
        ):
            with pytest.raises(KeyboardInterrupt):
                await app_module._session_cleanup_loop(interval_seconds=1)


# ── main ────────────────────────────────────────────────────────────


class TestMain:
    """Tests for the main entry point."""

    @pytest.mark.asyncio
    async def test_main_starts_socket_mode(self, app_module):
        """main() should start the AsyncSocketModeHandler."""

        def _close_coro(coro):
            """Close coroutines passed to create_task to avoid unawaited warnings."""
            coro.close()
            return MagicMock()

        with (
            patch("router.app.AsyncSocketModeHandler") as mock_handler_cls,
            patch("router.app.asyncio.create_task", side_effect=_close_coro),
        ):
            mock_handler = MagicMock()
            mock_handler.start_async = AsyncMock()
            mock_handler_cls.return_value = mock_handler

            await app_module.main()
            mock_handler.start_async.assert_called_once()
