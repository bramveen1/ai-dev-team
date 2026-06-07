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
    monkeypatch.setenv("LISA_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("LISA_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("LISA_SIGNING_SECRET", "test-secret")

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
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
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
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
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
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
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
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
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
            patch("router.app.find_session_by_thread", return_value=None),
            patch("router.app.create_session", return_value={"session_id": "s1"}),
            patch("router.app.dispatch", new_callable=AsyncMock, return_value={"response": "Hi!"}),
            patch("router.app.update_activity"),
            patch("router.app.add_to_thread_history"),
        ):
            await app_module._handle_event(event, say, client, receiving_agent="lisa", was_mentioned=True)
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
                "router.app.find_session_by_thread",
                return_value={"session_id": "s1", "agent_name": "lisa"},
            ),
            patch("router.app.update_activity"),
            patch("router.app.dispatch", new_callable=AsyncMock, return_value={"response": "Reply!"}),
            patch("router.app.add_to_thread_history"),
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
            with patch.object(app_module, "_handle_event", new_callable=AsyncMock) as mock_handle:
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
            with patch.object(app_module, "_handle_event", new_callable=AsyncMock) as mock_handle:
                await app_module.handle_message(event, say, client, receiving_agent="sam")
                mock_handle.assert_not_awaited()
        finally:
            app_module._bot_user_id_by_agent.clear()

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
            with patch.object(app_module, "_handle_event", new_callable=AsyncMock) as mock_handle:
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
        with patch.object(app_module, "_handle_event", new_callable=AsyncMock) as mock_handler:
            await on_app_mention({"text": "hi"}, say, client)
            mock_handler.assert_called_once_with({"text": "hi"}, say, client, receiving_agent="sam", was_mentioned=True)

    @pytest.mark.asyncio
    async def test_message_handler_routes_to_receiving_agent(self, app_module):
        """The factory-built message handler dispatches as the bound agent."""
        _, on_message = app_module._make_event_handlers("dave")
        say = AsyncMock()
        client = AsyncMock()
        with patch.object(app_module, "handle_message", new_callable=AsyncMock) as mock_handler:
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

        with patch.object(app_module, "_handle_event", new_callable=AsyncMock) as mock_handler:
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
                patch("router.app.pop_timed_out_sessions", return_value=[expired_session]),
                patch("router.app.handle_timeout_exit", new_callable=AsyncMock) as mock_timeout_exit,
                patch("router.app.get_agent_map", return_value={"lisa": {"container": "lisa", "name": "Lisa"}}),
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
        app_module._dispatch_bot_user_ids = set()
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
            # Workers bot id reaches the global allowlist every agent app shares.
            assert "U_BOT_WORKERS" in app_module._dispatch_bot_user_ids
            # End-to-end: a worker post would now pass the bot-message guard.
            assert app_module._is_dispatch_bot_sender({"user": "U_BOT_WORKERS"}, "lisa")
        finally:
            app_module._apps_by_agent.clear()
            app_module._app_tokens_by_agent.clear()
            app_module._bot_user_map.clear()
            app_module._bot_user_id_by_agent.clear()
            app_module._dispatch_bot_user_ids = set()

    @pytest.mark.asyncio
    async def test_main_skips_workers_seed_when_token_unset(self, app_module, monkeypatch):
        """No WORKERS_BOT_TOKEN → main() runs cleanly, the workers client is never
        constructed, and no workers entry is seeded into the allowlist (#252)."""

        def _close_coro(coro, **_kwargs):
            coro.close()
            return MagicMock()

        monkeypatch.delenv("WORKERS_BOT_TOKEN", raising=False)
        monkeypatch.delenv("DISPATCH_BOT_USER_IDS", raising=False)

        mock_app = MagicMock()
        mock_app.client.auth_test = AsyncMock(return_value={"user_id": "U_BOT_LISA"})
        app_module._apps_by_agent.clear()
        app_module._app_tokens_by_agent.clear()
        app_module._dispatch_bot_user_ids = set()
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
            app_module._dispatch_bot_user_ids = set()

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
        app_module._dispatch_bot_user_ids = set()
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
            app_module._dispatch_bot_user_ids = set()


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
                "router.app.get_agent_map",
                return_value={"sam": {"container": "sam", "name": "Sam"}},
            ),
            patch("router.app.find_session_by_thread", return_value=None),
            patch(
                "router.app.create_session",
                return_value={"session_id": "s1", "agent_name": "sam"},
            ),
            patch(
                "router.app.dispatch",
                new_callable=AsyncMock,
                return_value={"response": "Still working on it."},
            ) as mock_dispatch,
            patch("router.app.update_activity"),
            patch("router.app.add_to_thread_history"),
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
                "router.app.get_agent_map",
                return_value={"sam": {"container": "sam", "name": "Sam"}},
            ),
            patch("router.app.find_session_by_thread", return_value=None),
            patch(
                "router.app.create_session",
                return_value={"session_id": "s1", "agent_name": "sam"},
            ),
            patch(
                "router.app.dispatch",
                new_callable=AsyncMock,
                return_value={"response": "Dispatch is running…"},
            ) as mock_dispatch,
            patch("router.app.update_activity"),
            patch("router.app.add_to_thread_history"),
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
                "router.app.get_agent_map",
                return_value={"sam": {"container": "sam", "name": "Sam"}},
            ),
            patch("router.app.find_session_by_thread", return_value=None),
            patch(
                "router.app.create_session",
                return_value={"session_id": "s1", "agent_name": "sam"},
            ),
            patch(
                "router.app.dispatch",
                new_callable=AsyncMock,
                return_value={"response": "Still going…"},
            ) as mock_dispatch,
            patch("router.app.update_activity"),
            patch("router.app.add_to_thread_history"),
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
                "router.app.get_agent_map",
                return_value={"sam": {"container": "sam", "name": "Sam"}},
            ),
            patch("router.app.find_session_by_thread", return_value=None),
            patch(
                "router.app.create_session",
                return_value={"session_id": "s1", "agent_name": "sam"},
            ),
            patch(
                "router.app.dispatch",
                new_callable=AsyncMock,
                return_value={"response": "On it."},
            ) as mock_dispatch_direct,
            patch("router.app.update_activity"),
            patch("router.app.add_to_thread_history"),
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
                "router.app.get_agent_map",
                return_value={"sam": {"container": "sam", "name": "Sam"}},
            ),
            patch("router.app.find_session_by_thread", return_value=None),
            patch(
                "router.app.create_session",
                return_value={"session_id": "s1", "agent_name": "sam"},
            ),
            patch(
                "router.app.dispatch",
                new_callable=AsyncMock,
                return_value={"response": "Dispatch running."},
            ) as mock_dispatch_dispatch,
            patch("router.app.update_activity"),
            patch("router.app.add_to_thread_history"),
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
            patch(
                "router.app.get_agent_map",
                return_value={"lisa": {"container": "lisa-container", "name": "Lisa"}},
            ),
            patch(
                "router.app._run_in_container",
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
            patch(
                "router.app.get_agent_map",
                return_value={"lisa": {"container": "lisa-container", "name": "Lisa"}},
            ),
            patch(
                "router.app._run_in_container",
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
    async def test_execute_approved_draft_non_dispatch_falls_back_to_cli(self, app_module):
        """Approving a non-dispatch draft (e.g. github pr_merge) must use
        the existing agent CLI re-entry path via dispatch()."""
        draft = self._make_draft("github", "pr_merge", {"pr_url": "https://github.com/org/repo/pull/42"})
        client = AsyncMock()

        with (
            patch(
                "router.app.get_agent_map",
                return_value={"lisa": {"container": "lisa", "name": "Lisa"}},
            ),
            patch(
                "router.app.dispatch",
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
            patch(
                "router.app.get_agent_map",
                return_value={"lisa": {"container": "lisa-container", "name": "Lisa"}},
            ),
            patch(
                "router.app._run_in_container",
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
            patch(
                "router.app.get_agent_map",
                return_value={"lisa": {"container": "lisa-container", "name": "Lisa"}},
            ),
            patch(
                "router.app._run_in_container",
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
            patch("router.app.get_agent_map", return_value={}),
            patch("router.app._run_in_container", new_callable=AsyncMock) as mock_run,
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
            patch(
                "router.app.get_agent_map",
                return_value={"lisa": {"container": "lisa-container", "name": "Lisa"}},
            ),
            patch(
                "router.app._run_in_container",
                new_callable=AsyncMock,
                return_value=("Traceback (most recent call last):\n  ImportError: ...", "", 1),
            ),
        ):
            await app_module._execute_approved_draft(draft, "C001", "1.0", client)

        client.chat_postMessage.assert_called_once()
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "non-JSON" in text


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
        app_module._dispatch_bot_user_ids = {self._BOT_USER_ID}
        try:
            event = self._make_event(self._BOT_USER_ID, "dispatch review requested")
            say = AsyncMock()
            client = AsyncMock()

            with (
                patch(
                    "router.app.get_agent_map",
                    return_value={self._AGENT: {"container": "lisa", "name": "Lisa"}},
                ),
                patch("router.app.find_session_by_thread", return_value=None),
                patch(
                    "router.app.create_session",
                    return_value={"session_id": "s1", "agent_name": self._AGENT},
                ),
                patch(
                    "router.app.dispatch",
                    new_callable=AsyncMock,
                    return_value={"response": "LGTM"},
                ) as mock_dispatch,
                patch("router.app.update_activity"),
                patch("router.app.add_to_thread_history"),
            ):
                await app_module._handle_event(event, say, client, receiving_agent=self._AGENT, was_mentioned=True)
                mock_dispatch.assert_called_once()
                assert mock_dispatch.call_args.kwargs["agent_name"] == self._AGENT
        finally:
            app_module._bot_user_id_by_agent.pop(self._AGENT, None)
            app_module._dispatch_bot_user_ids = set()

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
        app_module._dispatch_bot_user_ids = {self._OWN_USER_ID}
        try:
            event = self._make_event(self._OWN_USER_ID, "review PR https://example.com/pr/1")
            say = AsyncMock()
            client = AsyncMock()

            with (
                patch(
                    "router.app.get_agent_map",
                    return_value={self._AGENT: {"container": "lisa", "name": "Lisa"}},
                ),
                patch("router.app.find_session_by_thread", return_value=None),
                patch(
                    "router.app.create_session",
                    return_value={"session_id": "s1", "agent_name": self._AGENT},
                ),
                patch(
                    "router.app.dispatch",
                    new_callable=AsyncMock,
                    return_value={"response": "LGTM"},
                ) as mock_dispatch,
                patch("router.app.update_activity"),
                patch("router.app.add_to_thread_history"),
            ):
                await app_module._handle_event(event, say, client, receiving_agent=self._AGENT, was_mentioned=True)
                mock_dispatch.assert_called_once()
        finally:
            app_module._bot_user_id_by_agent.pop(self._AGENT, None)
            app_module._dispatch_bot_user_ids = set()

    @pytest.mark.asyncio
    async def test_own_user_id_blocked_when_not_whitelisted(self, app_module):
        """Agent's own user ID is blocked when allowlist is empty (no auto-seed).

        Belt-and-braces: without the startup auto-seed having run (e.g. tests
        that don't invoke main()), an event from the agent's own user ID must
        still be dropped — the predicate has no special self-id logic, it
        relies purely on allowlist membership.
        """
        app_module._bot_user_id_by_agent[self._AGENT] = self._OWN_USER_ID
        app_module._dispatch_bot_user_ids = set()
        try:
            event = self._make_event(self._OWN_USER_ID)
            say = AsyncMock()
            client = AsyncMock()

            await app_module._handle_event(event, say, client, receiving_agent=self._AGENT, was_mentioned=True)
            say.assert_not_called()
        finally:
            app_module._bot_user_id_by_agent.pop(self._AGENT, None)
            app_module._dispatch_bot_user_ids = set()

    @pytest.mark.asyncio
    async def test_non_whitelisted_bot_blocked(self, app_module):
        """Bot message from a user ID not in the allowlist must be dropped."""
        app_module._bot_user_id_by_agent[self._AGENT] = self._OWN_USER_ID
        app_module._dispatch_bot_user_ids = {self._BOT_USER_ID}
        try:
            event = self._make_event(self._OTHER_USER_ID)
            say = AsyncMock()
            client = AsyncMock()

            await app_module._handle_event(event, say, client, receiving_agent=self._AGENT, was_mentioned=True)
            say.assert_not_called()
        finally:
            app_module._bot_user_id_by_agent.pop(self._AGENT, None)
            app_module._dispatch_bot_user_ids = set()

    @pytest.mark.asyncio
    async def test_empty_allowlist_blocks_all_bots(self, app_module):
        """When DISPATCH_BOT_USER_IDS is empty, all bot messages are dropped."""
        app_module._bot_user_id_by_agent[self._AGENT] = self._OWN_USER_ID
        app_module._dispatch_bot_user_ids = set()
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
        app_module._dispatch_bot_user_ids = {self._BOT_USER_ID}
        try:
            event = self._make_event(self._BOT_USER_ID, "hey please review PR https://example.com/pr/1 when ready")
            say = AsyncMock()
            client = AsyncMock()

            with (
                patch(
                    "router.app.get_agent_map",
                    return_value={self._AGENT: {"container": "lisa", "name": "Lisa"}},
                ),
                patch("router.app.find_session_by_thread", return_value=None),
                patch(
                    "router.app.create_session",
                    return_value={"session_id": "s1", "agent_name": self._AGENT},
                ),
                patch(
                    "router.app.dispatch",
                    new_callable=AsyncMock,
                    return_value={"response": "ok"},
                ) as mock_dispatch,
                patch("router.app.update_activity"),
                patch("router.app.add_to_thread_history"),
            ):
                await app_module._handle_event(event, say, client, receiving_agent=self._AGENT, was_mentioned=True)
                mock_dispatch.assert_called_once()
        finally:
            app_module._bot_user_id_by_agent.pop(self._AGENT, None)
            app_module._dispatch_bot_user_ids = set()

    @pytest.mark.asyncio
    async def test_no_user_field_blocked(self, app_module):
        """Bot event with no user field must be dropped even if allowlist is non-empty."""
        app_module._bot_user_id_by_agent[self._AGENT] = self._OWN_USER_ID
        app_module._dispatch_bot_user_ids = {self._BOT_USER_ID}
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
            app_module._dispatch_bot_user_ids = set()
