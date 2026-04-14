"""Unit tests for router.session_end — clean exit trigger detection and memory extraction.

These tests define the interface that router/session_end.py must implement.
Tests will SKIP until the module exists.
"""

import pytest

session_end = pytest.importorskip("router.session_end", reason="router.session_end not yet implemented")

pytestmark = pytest.mark.unit


class TestCleanExitTriggerDetection:
    """Tests for detecting clean exit triggers in messages."""

    @pytest.mark.parametrize(
        "message",
        [
            "thanks",
            "Thanks!",
            "thank you",
            "cheers",
            "Cheers!",
            "that's all",
            "That's all, thanks",
            "looks good, thanks!",
        ],
    )
    def test_detects_exit_triggers(self, message):
        """Should detect common exit trigger phrases."""
        assert session_end.is_exit_trigger(message) is True

    def test_trigger_is_case_insensitive(self):
        """Exit trigger detection should be case-insensitive."""
        assert session_end.is_exit_trigger("THANKS") is True
        assert session_end.is_exit_trigger("Thanks") is True
        assert session_end.is_exit_trigger("tHaNkS") is True
        assert session_end.is_exit_trigger("CHEERS") is True

    @pytest.mark.parametrize(
        "message",
        [
            "Can you fix the auth module?",
            "Please review this PR",
            "What does this function do?",
            "Let's refactor the database layer",
        ],
    )
    def test_non_exit_messages(self, message):
        """Regular work messages should not trigger exit."""
        assert session_end.is_exit_trigger(message) is False

    def test_empty_message_not_trigger(self):
        """An empty message should not be an exit trigger."""
        assert session_end.is_exit_trigger("") is False


class TestMemoryExtractionParsing:
    """Tests for parsing memory extraction from agent responses."""

    def test_extract_memory_from_response(self):
        """Should extract memory block from a structured agent response."""
        response = (
            "I've completed the auth review.\n\n"
            "## Memory\n"
            "- Reviewed auth module, found 2 issues\n"
            "- Suggested rate limiting addition\n"
        )
        result = session_end.extract_memory(response)
        assert "auth module" in result

    def test_extract_memory_no_block(self):
        """Should return empty string when no memory block is present."""
        response = "Done! The auth module looks good."
        result = session_end.extract_memory(response)
        assert result == ""

    def test_extract_memory_empty_response(self):
        """Should handle empty response gracefully."""
        result = session_end.extract_memory("")
        assert result == ""


class TestFormatThreadForPrompt:
    """Tests for _format_thread_for_prompt helper."""

    def test_formats_messages(self):
        """Should format thread history into [user]: text lines."""
        history = [
            {"user": "U001", "text": "Hello"},
            {"user": "U002", "text": "World"},
        ]
        result = session_end._format_thread_for_prompt(history)
        assert "[U001]: Hello" in result
        assert "[U002]: World" in result

    def test_empty_history(self):
        """Empty history should return empty string."""
        result = session_end._format_thread_for_prompt([])
        assert result == ""

    def test_missing_fields_use_defaults(self):
        """Messages with missing fields should use defaults."""
        history = [{"other": "field"}]
        result = session_end._format_thread_for_prompt(history)
        assert "[unknown]:" in result


class TestExtractJson:
    """Tests for _extract_json helper."""

    def test_direct_json(self):
        """Should parse direct JSON string."""
        result = session_end._extract_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_json_in_markdown_fence(self):
        """Should extract JSON from markdown code block."""
        text = 'Some preamble\n```json\n{"key": "value"}\n```\nAfter'
        result = session_end._extract_json(text)
        assert result == {"key": "value"}

    def test_json_in_plain_fence(self):
        """Should extract JSON from plain code block."""
        text = 'Preamble\n```\n{"key": "value"}\n```'
        result = session_end._extract_json(text)
        assert result == {"key": "value"}

    def test_json_object_in_text(self):
        """Should extract first JSON object from surrounding text."""
        text = 'Here is the data: {"key": "value"} and more text'
        result = session_end._extract_json(text)
        assert result == {"key": "value"}

    def test_no_json_returns_empty_dict(self):
        """Should return empty dict when no JSON is found."""
        result = session_end._extract_json("no json here")
        assert result == {}

    def test_empty_string_input(self):
        """Should handle empty string input gracefully."""
        result = session_end._extract_json("")
        assert result == {}

    def test_invalid_json_in_fence(self):
        """Should handle invalid JSON in code fence gracefully."""
        text = "```json\nnot valid json\n```"
        result = session_end._extract_json(text)
        assert result == {}


class TestInvokeCliForExtraction:
    """Tests for _invoke_cli_for_extraction."""

    @pytest.mark.asyncio
    async def test_successful_extraction(self):
        """Should parse JSON from CLI result field."""
        import json
        from unittest.mock import AsyncMock, patch

        cli_stdout = json.dumps({"result": '{"decisions": [], "agent_memory": "learned stuff"}'})
        with patch("router.session_end._run_in_container", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (cli_stdout, "", 0)
            result = await session_end._invoke_cli_for_extraction("test-container", "test prompt")
            assert result.get("agent_memory") == "learned stuff"

    @pytest.mark.asyncio
    async def test_cli_failure_returns_empty(self):
        """CLI exception should return empty dict."""
        from unittest.mock import AsyncMock, patch

        with patch("router.session_end._run_in_container", new_callable=AsyncMock, side_effect=Exception("boom")):
            result = await session_end._invoke_cli_for_extraction("container", "prompt")
            assert result == {}

    @pytest.mark.asyncio
    async def test_nonzero_exit_returns_empty(self):
        """Non-zero exit code should return empty dict."""
        from unittest.mock import AsyncMock, patch

        with patch("router.session_end._run_in_container", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ("", "error", 1)
            result = await session_end._invoke_cli_for_extraction("container", "prompt")
            assert result == {}

    @pytest.mark.asyncio
    async def test_empty_stdout_returns_empty(self):
        """Empty stdout should return empty dict."""
        from unittest.mock import AsyncMock, patch

        with patch("router.session_end._run_in_container", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ("  ", "", 0)
            result = await session_end._invoke_cli_for_extraction("container", "prompt")
            assert result == {}

    @pytest.mark.asyncio
    async def test_non_json_stdout_falls_back(self):
        """Non-JSON stdout should try to extract JSON from raw text."""
        from unittest.mock import AsyncMock, patch

        with patch("router.session_end._run_in_container", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ('raw text with {"key": "val"} in it', "", 0)
            result = await session_end._invoke_cli_for_extraction("container", "prompt")
            assert result.get("key") == "val"

    @pytest.mark.asyncio
    async def test_unparseable_result_returns_empty(self):
        """If result text has no JSON, return empty dict."""
        import json
        from unittest.mock import AsyncMock, patch

        cli_stdout = json.dumps({"result": "no json here at all"})
        with patch("router.session_end._run_in_container", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (cli_stdout, "", 0)
            result = await session_end._invoke_cli_for_extraction("container", "prompt")
            assert result == {}


class TestHandleCleanExit:
    """Tests for handle_clean_exit."""

    @pytest.mark.asyncio
    async def test_persists_memory(self):
        """Should extract memory via CLI and persist it."""
        from unittest.mock import AsyncMock, MagicMock, patch

        with (
            patch(
                "router.session_end._invoke_cli_for_extraction",
                new_callable=AsyncMock,
                return_value={"decisions": [{"date": "2024-01-20", "topic": "auth", "content": "approved"}]},
            ),
            patch("router.session_end.persist_memory", return_value=1) as mock_persist,
        ):
            count = await session_end.handle_clean_exit(
                agent_name="lisa",
                container="lisa",
                thread_history=[{"user": "U001", "text": "thanks"}],
                slack_client=MagicMock(),
                channel="C001",
                thread_ts="1.0",
            )
            assert count == 1
            mock_persist.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_zero_on_error(self):
        """Should return 0 if extraction or persistence fails."""
        from unittest.mock import AsyncMock, MagicMock, patch

        with patch(
            "router.session_end._invoke_cli_for_extraction",
            new_callable=AsyncMock,
            side_effect=Exception("boom"),
        ):
            count = await session_end.handle_clean_exit(
                agent_name="lisa",
                container="lisa",
                thread_history=[],
                slack_client=MagicMock(),
                channel="C001",
                thread_ts="1.0",
            )
            assert count == 0


class TestHandleTimeoutExit:
    """Tests for handle_timeout_exit."""

    @pytest.mark.asyncio
    async def test_posts_summary_and_persists_memory(self):
        """Should post a thread summary and persist memory."""
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_client = MagicMock()
        mock_client.chat_postMessage = AsyncMock()

        summary_data = {
            "topic": "auth review",
            "key_points": "found bugs",
            "open_question": "rate limiting",
            "pending_action": "PR review",
        }
        memory_data = {"agent_memory": "reviewed auth"}

        call_count = 0

        async def mock_invoke(container, prompt, timeout=60):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return summary_data
            return memory_data

        with (
            patch("router.session_end._invoke_cli_for_extraction", side_effect=mock_invoke),
            patch("router.session_end.persist_memory", return_value=1),
        ):
            count = await session_end.handle_timeout_exit(
                agent_name="lisa",
                container="lisa",
                thread_history=[{"user": "U001", "text": "hello"}],
                slack_client=mock_client,
                channel="C001",
                thread_ts="1.0",
            )
            mock_client.chat_postMessage.assert_called_once()
            msg = mock_client.chat_postMessage.call_args[1]["text"]
            assert "auth review" in msg
            assert count == 1

    @pytest.mark.asyncio
    async def test_summary_error_still_persists_memory(self):
        """If summary posting fails, memory should still be persisted."""
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_client = MagicMock()
        mock_client.chat_postMessage = AsyncMock(side_effect=Exception("slack error"))

        call_count = 0

        async def mock_invoke(container, prompt, timeout=60):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"topic": "test"}
            return {"agent_memory": "stuff"}

        with (
            patch("router.session_end._invoke_cli_for_extraction", side_effect=mock_invoke),
            patch("router.session_end.persist_memory", return_value=1),
        ):
            count = await session_end.handle_timeout_exit(
                agent_name="lisa",
                container="lisa",
                thread_history=[],
                slack_client=mock_client,
                channel="C001",
                thread_ts="1.0",
            )
            assert count == 1

    @pytest.mark.asyncio
    async def test_memory_error_returns_zero(self):
        """If memory persistence fails, should return 0."""
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_client = MagicMock()
        mock_client.chat_postMessage = AsyncMock()

        call_count = 0

        async def mock_invoke(container, prompt, timeout=60):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"topic": "test"}
            raise Exception("extraction failed")

        with patch("router.session_end._invoke_cli_for_extraction", side_effect=mock_invoke):
            count = await session_end.handle_timeout_exit(
                agent_name="lisa",
                container="lisa",
                thread_history=[],
                slack_client=mock_client,
                channel="C001",
                thread_ts="1.0",
            )
            assert count == 0
