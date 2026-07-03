"""Unit tests for router.dispatcher — Claude CLI dispatch and error handling.

Tests mock ``_run_in_container`` so no Docker daemon is required.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router.dispatcher import (
    CONTAINER_AGENT_MEMORY_FILE,
    CONTAINER_ORG_MEMORY_FILE,
    CONTAINER_WORLDVIEW_FILE,
    DEFAULT_MAX_TOKEN_BUDGET,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_CONTEXT_TOKENS_ENV,
    DispatchError,
    DispatchTimeoutError,
    _resolve_token_budget,
    _run_in_container,
    dispatch,
)

pytestmark = pytest.mark.unit

# Standard successful CLI JSON response
_CLI_RESULT_TEXT = "I'm Lisa, the project manager. How can I help you today?"
_MOCK_CLI_STDOUT = json.dumps(
    {
        "result": _CLI_RESULT_TEXT,
        "session_id": "test-session-00000000",
        "total_cost_usd": 0.012,
        "usage": {"input_tokens": 120, "output_tokens": 58},
    }
)


@pytest.fixture(autouse=True)
def mock_thread_loader():
    """Mock load_thread_history to return an empty thread (no history)."""
    with patch("router.dispatcher.load_thread_history", new_callable=AsyncMock) as mock:
        mock.return_value = []
        yield mock


@pytest.fixture()
def mock_container():
    """Mock _run_in_container to return a successful CLI response."""
    with patch("router.dispatcher._run_in_container", new_callable=AsyncMock) as mock:
        mock.return_value = (_MOCK_CLI_STDOUT, "", 0)
        yield mock


# ── Routing ──────────────────────────────────────────────────────────


class TestDispatchRouting:
    """Tests for routing messages to the correct agent."""

    @pytest.mark.asyncio
    async def test_dispatch_routes_to_lisa(self, mock_slack_client, mock_container):
        """Mentioning 'lisa' should route to the Lisa agent."""
        result = await dispatch(
            agent_name="lisa",
            message="Please review the auth module",
            channel="C0001",
            thread_ts="1705700000.000100",
            client=mock_slack_client,
        )
        assert result is not None
        assert result.get("agent") == "lisa"

    @pytest.mark.asyncio
    async def test_dispatch_returns_response(self, mock_slack_client, mock_container):
        """Dispatch should return a response dict with expected keys."""
        result = await dispatch(
            agent_name="lisa",
            message="Fix the bug in auth.py",
            channel="C0001",
            thread_ts="1705700000.000100",
            client=mock_slack_client,
        )
        assert result["status"] == "ok"
        assert result["response"] == _CLI_RESULT_TEXT

    @pytest.mark.asyncio
    async def test_dispatch_invokes_container_with_correct_command(self, mock_slack_client, mock_container):
        """The docker exec command should include the right CLI flags."""
        await dispatch(
            agent_name="lisa",
            message="Hello Lisa",
            channel="C0001",
            thread_ts="1705700000.000100",
            client=mock_slack_client,
        )
        mock_container.assert_called_once()
        container, cli_cmd, _timeout = mock_container.call_args[0]
        stdin_data = mock_container.call_args[1].get("stdin_data", "")
        assert container == "lisa"
        assert cli_cmd[0] == "claude"
        assert "-p" in cli_cmd
        # The prompt is piped via stdin, not as a CLI argument
        assert "Hello Lisa" in stdin_data
        assert "--bare" not in cli_cmd  # --bare blocks OAuth/Max subscription auth
        assert "--output-format" in cli_cmd and "json" in cli_cmd
        # role.md replaces Claude Code's default identity prompt
        assert "--system-prompt-file" in cli_cmd
        role_idx = cli_cmd.index("--system-prompt-file")
        assert cli_cmd[role_idx + 1] == "/config/agents/lisa/role.md"
        assert "--no-session-persistence" in cli_cmd
        assert "--max-turns" in cli_cmd

    @pytest.mark.asyncio
    async def test_dispatch_includes_soul_system_prompt_files(self, mock_slack_client, mock_container):
        """CLI command should include WORLDVIEW, personality, memory, and org memory files."""
        await dispatch(
            agent_name="lisa",
            message="Hello Lisa",
            channel="C0001",
            thread_ts="1705700000.000100",
            client=mock_slack_client,
        )
        _, cli_cmd, _ = mock_container.call_args[0]
        assert CONTAINER_WORLDVIEW_FILE in cli_cmd
        assert "/config/agents/lisa/personality.md" in cli_cmd
        assert CONTAINER_AGENT_MEMORY_FILE.format(agent="lisa") in cli_cmd
        assert CONTAINER_ORG_MEMORY_FILE in cli_cmd

    @pytest.mark.asyncio
    async def test_dispatch_system_prompt_file_order(self, mock_slack_client, mock_container):
        """role.md replaces the default system prompt; the rest append in order:
        WORLDVIEW -> personality -> agent memory -> org memory."""
        await dispatch(
            agent_name="lisa",
            message="Hello Lisa",
            channel="C0001",
            thread_ts="1705700000.000100",
            client=mock_slack_client,
        )
        _, cli_cmd, _ = mock_container.call_args[0]
        # role.md is set via --system-prompt-file (replace), not append
        sysprompt_idx = cli_cmd.index("--system-prompt-file")
        assert cli_cmd[sysprompt_idx + 1] == "/config/agents/lisa/role.md"
        # The rest are appended in order
        worldview_idx = cli_cmd.index(CONTAINER_WORLDVIEW_FILE)
        personality_idx = cli_cmd.index("/config/agents/lisa/personality.md")
        agent_mem_idx = cli_cmd.index(CONTAINER_AGENT_MEMORY_FILE.format(agent="lisa"))
        org_mem_idx = cli_cmd.index(CONTAINER_ORG_MEMORY_FILE)
        assert sysprompt_idx < worldview_idx < personality_idx < agent_mem_idx < org_mem_idx


# ── Error handling ───────────────────────────────────────────────────


class TestDispatchErrorHandling:
    """Tests for error handling in dispatch."""

    @pytest.mark.asyncio
    async def test_unknown_agent_raises_error(self, mock_slack_client):
        """Dispatching to an unknown agent should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown agent"):
            await dispatch(
                agent_name="nonexistent_agent",
                message="Hello",
                channel="C0001",
                thread_ts="1705700000.000100",
                client=mock_slack_client,
            )

    @pytest.mark.asyncio
    async def test_empty_message_raises_error(self, mock_slack_client):
        """Dispatching with an empty message should raise ValueError."""
        with pytest.raises(ValueError):
            await dispatch(
                agent_name="lisa",
                message="",
                channel="C0001",
                thread_ts="1705700000.000100",
                client=mock_slack_client,
            )

    @pytest.mark.asyncio
    async def test_whitespace_message_raises_error(self, mock_slack_client):
        """Whitespace-only messages should raise ValueError."""
        with pytest.raises(ValueError):
            await dispatch(
                agent_name="lisa",
                message="   ",
                channel="C0001",
                thread_ts="1705700000.000100",
                client=mock_slack_client,
            )

    @pytest.mark.asyncio
    async def test_cli_nonzero_exit_raises_error(self, mock_slack_client):
        """Non-zero CLI exit code should raise DispatchError."""
        with patch("router.dispatcher._run_in_container", new_callable=AsyncMock) as mock:
            mock.return_value = ("", "Error: invalid API key", 1)
            with pytest.raises(DispatchError, match="exited with code 1"):
                await dispatch(
                    agent_name="lisa",
                    message="Hello",
                    channel="C0001",
                    thread_ts="1705700000.000100",
                    client=mock_slack_client,
                )

    @pytest.mark.asyncio
    async def test_empty_stdout_raises_error(self, mock_slack_client):
        """Empty CLI stdout should raise DispatchError."""
        with patch("router.dispatcher._run_in_container", new_callable=AsyncMock) as mock:
            mock.return_value = ("", "", 0)
            with pytest.raises(DispatchError, match="empty response"):
                await dispatch(
                    agent_name="lisa",
                    message="Hello",
                    channel="C0001",
                    thread_ts="1705700000.000100",
                    client=mock_slack_client,
                )

    @pytest.mark.asyncio
    async def test_invalid_json_raises_error(self, mock_slack_client):
        """Non-JSON CLI output should raise DispatchError."""
        with patch("router.dispatcher._run_in_container", new_callable=AsyncMock) as mock:
            mock.return_value = ("this is not json", "", 0)
            with pytest.raises(DispatchError, match="invalid JSON"):
                await dispatch(
                    agent_name="lisa",
                    message="Hello",
                    channel="C0001",
                    thread_ts="1705700000.000100",
                    client=mock_slack_client,
                )

    @pytest.mark.asyncio
    async def test_empty_result_field_raises_error(self, mock_slack_client):
        """JSON with an empty 'result' field should raise DispatchError."""
        with patch("router.dispatcher._run_in_container", new_callable=AsyncMock) as mock:
            mock.return_value = (json.dumps({"result": "", "session_id": "x"}), "", 0)
            with pytest.raises(DispatchError, match="empty result"):
                await dispatch(
                    agent_name="lisa",
                    message="Hello",
                    channel="C0001",
                    thread_ts="1705700000.000100",
                    client=mock_slack_client,
                )

    @pytest.mark.asyncio
    async def test_missing_result_field_raises_error(self, mock_slack_client):
        """JSON without a 'result' key should raise DispatchError."""
        with patch("router.dispatcher._run_in_container", new_callable=AsyncMock) as mock:
            mock.return_value = (json.dumps({"session_id": "x"}), "", 0)
            with pytest.raises(DispatchError, match="empty result"):
                await dispatch(
                    agent_name="lisa",
                    message="Hello",
                    channel="C0001",
                    thread_ts="1705700000.000100",
                    client=mock_slack_client,
                )


# ── Timeout handling ─────────────────────────────────────────────────


class TestDispatchTimeout:
    """Tests for dispatch timeout handling."""

    @pytest.mark.asyncio
    async def test_dispatch_respects_custom_timeout(self, mock_slack_client, mock_container):
        """Custom timeout should be forwarded to _run_in_container."""
        result = await dispatch(
            agent_name="lisa",
            message="Quick task",
            channel="C0001",
            thread_ts="1705700000.000100",
            client=mock_slack_client,
            timeout=30,
        )
        assert result is not None
        _, _, timeout_used = mock_container.call_args[0]
        assert timeout_used == 30

    @pytest.mark.asyncio
    async def test_dispatch_uses_default_timeout(self, mock_slack_client, mock_container):
        """Omitting timeout should use DEFAULT_TIMEOUT_SECONDS."""
        await dispatch(
            agent_name="lisa",
            message="Hello",
            channel="C0001",
            thread_ts="1705700000.000100",
            client=mock_slack_client,
        )
        _, _, timeout_used = mock_container.call_args[0]
        assert timeout_used == DEFAULT_TIMEOUT_SECONDS

    @pytest.mark.asyncio
    async def test_cli_timeout_raises_dispatch_timeout_error(self, mock_slack_client):
        """DispatchTimeoutError from _run_in_container should propagate."""
        with patch("router.dispatcher._run_in_container", new_callable=AsyncMock) as mock:
            mock.side_effect = DispatchTimeoutError("timed out after 30s")
            with pytest.raises(DispatchTimeoutError):
                await dispatch(
                    agent_name="lisa",
                    message="Hello",
                    channel="C0001",
                    thread_ts="1705700000.000100",
                    client=mock_slack_client,
                )

    @pytest.mark.asyncio
    async def test_per_agent_container_timeout_overrides_param(self, mock_slack_client, mock_container):
        """container_timeout in agent config takes precedence over the timeout param."""
        with patch("router.dispatcher.get_agent_map") as mock_map:
            mock_map.return_value = {
                "lisa": {
                    "name": "Lisa",
                    "container": "lisa",
                    "container_timeout": 900,
                }
            }
            await dispatch(
                agent_name="lisa",
                message="Hello",
                channel="C0001",
                thread_ts="1705700000.000100",
                client=mock_slack_client,
                timeout=30,  # should be overridden by container_timeout=900
            )
        _, _, timeout_used = mock_container.call_args[0]
        assert timeout_used == 900

    @pytest.mark.asyncio
    async def test_global_timeout_used_when_no_per_agent_timeout(self, mock_slack_client, mock_container):
        """When agent config has no container_timeout, the timeout param is used."""
        with patch("router.dispatcher.get_agent_map") as mock_map:
            mock_map.return_value = {
                "lisa": {
                    "name": "Lisa",
                    "container": "lisa",
                    "container_timeout": None,
                }
            }
            await dispatch(
                agent_name="lisa",
                message="Hello",
                channel="C0001",
                thread_ts="1705700000.000100",
                client=mock_slack_client,
                timeout=1800,
            )
        _, _, timeout_used = mock_container.call_args[0]
        assert timeout_used == 1800

    @pytest.mark.asyncio
    async def test_timeout_posts_slack_notification(self, mock_slack_client):
        """On DispatchTimeoutError, a Slack notification should be posted in the thread."""
        with (
            patch("router.dispatcher._run_in_container", new_callable=AsyncMock) as mock_container,
            patch("router.dispatcher._post_stuck_notification", new_callable=AsyncMock) as mock_notify,
        ):
            mock_container.side_effect = DispatchTimeoutError("timed out after 1800s")
            with pytest.raises(DispatchTimeoutError):
                await dispatch(
                    agent_name="lisa",
                    message="Hello",
                    channel="C0001",
                    thread_ts="1705700000.000100",
                    client=mock_slack_client,
                    timeout=1800,
                )
        mock_notify.assert_called_once()
        call_kwargs = mock_notify.call_args[1]
        assert call_kwargs["channel"] == "C0001"
        assert call_kwargs["thread_ts"] == "1705700000.000100"
        assert "hit the router timeout" in call_kwargs["text"]
        assert "1800s" in call_kwargs["text"]
        assert "Likely needs the timeout raised" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_timeout_notification_includes_agent_name(self, mock_slack_client):
        """The timeout Slack message should name the agent that was killed."""
        with (
            patch("router.dispatcher._run_in_container", new_callable=AsyncMock) as mock_container,
            patch("router.dispatcher._post_stuck_notification", new_callable=AsyncMock) as mock_notify,
        ):
            mock_container.side_effect = DispatchTimeoutError("timed out")
            with pytest.raises(DispatchTimeoutError):
                await dispatch(
                    agent_name="lisa",
                    message="Hello",
                    channel="C0001",
                    thread_ts="1705700000.000100",
                    client=mock_slack_client,
                )
        text = mock_notify.call_args[1]["text"]
        assert "Lisa" in text


# ── Thread awareness ────────────────────────────────────────────────


class TestDispatchThreadAwareness:
    """Tests for thread history loading and context building in dispatch."""

    @pytest.mark.asyncio
    async def test_dispatch_loads_thread_history(self, mock_slack_client, mock_container, mock_thread_loader):
        """Dispatch should call load_thread_history with the correct args."""
        await dispatch(
            agent_name="lisa",
            message="Follow up question",
            channel="C0001",
            thread_ts="1705700000.000100",
            client=mock_slack_client,
        )
        mock_thread_loader.assert_called_once_with(
            client=mock_slack_client,
            channel="C0001",
            thread_ts="1705700000.000100",
            max_messages=20,
        )

    @pytest.mark.asyncio
    async def test_dispatch_includes_thread_context_in_prompt(self, mock_slack_client):
        """When thread history exists, the CLI prompt should include conversation context."""
        with (
            patch("router.dispatcher.load_thread_history", new_callable=AsyncMock) as mock_loader,
            patch("router.dispatcher._run_in_container", new_callable=AsyncMock) as mock_run,
        ):
            mock_loader.return_value = [
                {"user": "U0001", "text": "Can you check my calendar?", "ts": "1.0"},
                {"user": "U_BOT", "text": "You have 3 meetings.", "ts": "2.0"},
            ]
            mock_run.return_value = (_MOCK_CLI_STDOUT, "", 0)

            await dispatch(
                agent_name="lisa",
                message="Move the 2pm to Thursday",
                channel="C0001",
                thread_ts="1.0",
                client=mock_slack_client,
            )

            # Check the prompt piped via stdin includes conversation history and current message
            prompt = mock_run.call_args[1].get("stdin_data", "")
            assert "CONVERSATION HISTORY" in prompt
            assert "Can you check my calendar?" in prompt
            assert "You have 3 meetings." in prompt
            assert "Move the 2pm to Thursday" in prompt

    @pytest.mark.asyncio
    async def test_dispatch_no_thread_sends_plain_message(self, mock_slack_client, mock_container, mock_thread_loader):
        """When there is no thread history, the message should still be in the prompt."""
        mock_thread_loader.return_value = []
        await dispatch(
            agent_name="lisa",
            message="Hello Lisa",
            channel="C0001",
            thread_ts="1705700000.000100",
            client=mock_slack_client,
        )
        prompt = mock_container.call_args[1].get("stdin_data", "")
        assert "Hello Lisa" in prompt
        assert "CONVERSATION HISTORY" not in prompt

    @pytest.mark.asyncio
    async def test_dispatch_truncates_long_thread_context(self, mock_slack_client):
        """Long thread context should be truncated to fit within token budget."""
        with (
            patch("router.dispatcher.load_thread_history", new_callable=AsyncMock) as mock_loader,
            patch("router.dispatcher._run_in_container", new_callable=AsyncMock) as mock_run,
        ):
            # Create a very long thread history
            long_history = [{"user": "U0001", "text": "x" * 500, "ts": str(float(i))} for i in range(50)]
            mock_loader.return_value = long_history
            mock_run.return_value = (_MOCK_CLI_STDOUT, "", 0)

            await dispatch(
                agent_name="lisa",
                message="Latest question",
                channel="C0001",
                thread_ts="0.0",
                client=mock_slack_client,
                max_token_budget=100,
            )

            # Prompt is piped via stdin
            prompt = mock_run.call_args[1].get("stdin_data", "")
            # Over-budget context should have thread history dropped
            assert "CONVERSATION HISTORY" not in prompt
            assert "Latest question" in prompt

    @pytest.mark.asyncio
    async def test_dispatch_respects_max_thread_messages(self, mock_slack_client, mock_container):
        """Custom max_thread_messages should be forwarded to load_thread_history."""
        with patch("router.dispatcher.load_thread_history", new_callable=AsyncMock) as mock_loader:
            mock_loader.return_value = []
            await dispatch(
                agent_name="lisa",
                message="Hello",
                channel="C0001",
                thread_ts="1.0",
                client=mock_slack_client,
                max_thread_messages=5,
            )
            mock_loader.assert_called_once_with(
                client=mock_slack_client,
                channel="C0001",
                thread_ts="1.0",
                max_messages=5,
            )


# ── Token budget resolution ─────────────────────────────────────────


class TestTokenBudgetResolution:
    """Tests for _resolve_token_budget — explicit arg > env > default."""

    def test_default_when_unset(self, monkeypatch):
        """Unset env and no explicit arg falls back to the default."""
        monkeypatch.delenv(MAX_CONTEXT_TOKENS_ENV, raising=False)
        assert _resolve_token_budget(None) == DEFAULT_MAX_TOKEN_BUDGET

    def test_default_constant_value(self):
        """The default budget should be 32000 (per issue #110 section 0)."""
        assert DEFAULT_MAX_TOKEN_BUDGET == 32000

    def test_env_var_honored_when_valid(self, monkeypatch):
        """A valid positive int in the env should be used."""
        monkeypatch.setenv(MAX_CONTEXT_TOKENS_ENV, "12345")
        assert _resolve_token_budget(None) == 12345

    def test_explicit_arg_overrides_env(self, monkeypatch):
        """An explicit arg should beat the env var."""
        monkeypatch.setenv(MAX_CONTEXT_TOKENS_ENV, "12345")
        assert _resolve_token_budget(500) == 500

    def test_empty_env_falls_back_to_default(self, monkeypatch):
        """Empty/whitespace env value falls back to the default."""
        monkeypatch.setenv(MAX_CONTEXT_TOKENS_ENV, "")
        assert _resolve_token_budget(None) == DEFAULT_MAX_TOKEN_BUDGET
        monkeypatch.setenv(MAX_CONTEXT_TOKENS_ENV, "   ")
        assert _resolve_token_budget(None) == DEFAULT_MAX_TOKEN_BUDGET

    def test_invalid_env_falls_back_and_warns(self, monkeypatch, caplog):
        """Non-int env value should warn (in the settings layer) and fall back to default."""
        monkeypatch.setenv(MAX_CONTEXT_TOKENS_ENV, "not-a-number")
        with caplog.at_level("WARNING", logger="router.settings"):
            result = _resolve_token_budget(None)
        assert result == DEFAULT_MAX_TOKEN_BUDGET
        assert any("invalid" in r.message.lower() and MAX_CONTEXT_TOKENS_ENV in r.message for r in caplog.records)

    def test_zero_or_negative_env_falls_back_and_warns(self, monkeypatch, caplog):
        """Non-positive env value should warn (in the settings layer) and fall back to default."""
        monkeypatch.setenv(MAX_CONTEXT_TOKENS_ENV, "0")
        with caplog.at_level("WARNING", logger="router.settings"):
            result = _resolve_token_budget(None)
        assert result == DEFAULT_MAX_TOKEN_BUDGET
        assert any("must be >= 1" in r.message for r in caplog.records)

        caplog.clear()
        monkeypatch.setenv(MAX_CONTEXT_TOKENS_ENV, "-100")
        with caplog.at_level("WARNING", logger="router.settings"):
            result = _resolve_token_budget(None)
        assert result == DEFAULT_MAX_TOKEN_BUDGET
        assert any("must be >= 1" in r.message for r in caplog.records)


# ── Token budget end-to-end via dispatch ────────────────────────────


class TestDispatchTokenBudgetEnv:
    """Verify dispatch() actually picks up MAX_CONTEXT_TOKENS."""

    @pytest.mark.asyncio
    async def test_dispatch_uses_env_var_budget(self, mock_slack_client, mock_container, monkeypatch):
        """When MAX_CONTEXT_TOKENS is set, build_full_context receives it."""
        monkeypatch.setenv(MAX_CONTEXT_TOKENS_ENV, "16000")
        with patch("router.dispatcher.build_full_context", return_value="ctx") as mock_build:
            await dispatch(
                agent_name="lisa",
                message="Hello",
                channel="C0001",
                thread_ts="1.0",
                client=mock_slack_client,
            )
        assert mock_build.call_args.kwargs["max_tokens"] == 16000

    @pytest.mark.asyncio
    async def test_dispatch_uses_default_when_env_unset(self, mock_slack_client, mock_container, monkeypatch):
        """Unset env falls back to DEFAULT_MAX_TOKEN_BUDGET."""
        monkeypatch.delenv(MAX_CONTEXT_TOKENS_ENV, raising=False)
        with patch("router.dispatcher.build_full_context", return_value="ctx") as mock_build:
            await dispatch(
                agent_name="lisa",
                message="Hello",
                channel="C0001",
                thread_ts="1.0",
                client=mock_slack_client,
            )
        assert mock_build.call_args.kwargs["max_tokens"] == DEFAULT_MAX_TOKEN_BUDGET

    @pytest.mark.asyncio
    async def test_dispatch_invalid_env_falls_back_to_default(self, mock_slack_client, mock_container, monkeypatch):
        """Invalid env var should fall back to DEFAULT_MAX_TOKEN_BUDGET."""
        monkeypatch.setenv(MAX_CONTEXT_TOKENS_ENV, "garbage")
        with patch("router.dispatcher.build_full_context", return_value="ctx") as mock_build:
            await dispatch(
                agent_name="lisa",
                message="Hello",
                channel="C0001",
                thread_ts="1.0",
                client=mock_slack_client,
            )
        assert mock_build.call_args.kwargs["max_tokens"] == DEFAULT_MAX_TOKEN_BUDGET

    @pytest.mark.asyncio
    async def test_explicit_arg_overrides_env(self, mock_slack_client, mock_container, monkeypatch):
        """An explicit max_token_budget arg should override the env var."""
        monkeypatch.setenv(MAX_CONTEXT_TOKENS_ENV, "16000")
        with patch("router.dispatcher.build_full_context", return_value="ctx") as mock_build:
            await dispatch(
                agent_name="lisa",
                message="Hello",
                channel="C0001",
                thread_ts="1.0",
                client=mock_slack_client,
                max_token_budget=2048,
            )
        assert mock_build.call_args.kwargs["max_tokens"] == 2048


# ── Per-persona model pin ────────────────────────────────────────────


_PINNED_AGENT_MAP = {
    "lisa": {
        "name": "Lisa",
        "container": "lisa",
        "role_file": "config/agents/lisa/role.md",
        "personality_file": "config/agents/lisa/personality.md",
        "thinking_status": "",
        "model": "opus",
    }
}

_UNPINNED_AGENT_MAP = {
    "lisa": {
        "name": "Lisa",
        "container": "lisa",
        "role_file": "config/agents/lisa/role.md",
        "personality_file": "config/agents/lisa/personality.md",
        "thinking_status": "",
        "model": None,
    }
}


class TestPersonaModelPin:
    """Tests for per-persona model pinning via agent.yaml `model:` field."""

    @pytest.mark.asyncio
    async def test_model_pin_adds_model_flag(self, mock_slack_client, mock_container):
        """When agent.yaml has `model: opus`, --model opus must appear in the CLI command."""
        with patch("router.dispatcher.get_agent_map", return_value=_PINNED_AGENT_MAP):
            await dispatch(
                agent_name="lisa",
                message="Hello",
                channel="C0001",
                thread_ts="1.0",
                client=mock_slack_client,
            )
        _container, cli_cmd, _timeout = mock_container.call_args[0]
        assert "--model" in cli_cmd
        model_idx = cli_cmd.index("--model")
        assert cli_cmd[model_idx + 1] == "opus"

    @pytest.mark.asyncio
    async def test_no_model_pin_omits_model_flag(self, mock_slack_client, mock_container):
        """When agent.yaml has no `model:`, --model must not appear in the CLI command."""
        with patch("router.dispatcher.get_agent_map", return_value=_UNPINNED_AGENT_MAP):
            await dispatch(
                agent_name="lisa",
                message="Hello",
                channel="C0001",
                thread_ts="1.0",
                client=mock_slack_client,
            )
        _container, cli_cmd, _timeout = mock_container.call_args[0]
        assert "--model" not in cli_cmd


# ── Container-side kill on timeout ──────────────────────────────────


def _make_fake_proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    """Build a minimal fake asyncio.Process using real coroutines.

    Plain ``async def`` functions are used instead of AsyncMock so that no
    unawaited internal coroutine is left behind — which would be treated as an
    error by the project's ``filterwarnings = ["error"]`` config.
    """
    _stdout, _stderr = stdout, stderr

    proc = MagicMock()

    async def _communicate(input=None):
        return (_stdout, _stderr)

    proc.communicate = _communicate
    proc.kill = MagicMock()
    proc.returncode = returncode
    return proc


class TestRunInContainerContainerSideKill:
    """Regression tests for the container-side kill path in _run_in_container.

    No real Docker daemon is required — asyncio.create_subprocess_exec is
    monkeypatched so only the command construction and error mapping are exercised.
    """

    @pytest.mark.asyncio
    async def test_docker_exec_command_wraps_with_coreutils_timeout(self):
        """docker exec must invoke coreutils timeout(1) inside the container.

        This ensures the kill happens in the container's PID namespace and
        prevents orphaned claude -p processes after a router-side timeout.
        """
        fake_proc = _make_fake_proc(returncode=0, stdout=b"stdout")
        captured_args: list = []

        async def fake_create(*args, **kwargs):
            captured_args.extend(args)
            return fake_proc

        with patch("asyncio.create_subprocess_exec", fake_create):
            await _run_in_container("mycontainer", ["claude", "-p"], timeout=60)

        container_idx = captured_args.index("mycontainer")
        in_container_cmd = captured_args[container_idx + 1 :]
        assert in_container_cmd[0] == "timeout", "container-side command must start with 'timeout'"
        assert in_container_cmd[1] == "60", "timeout value must match the requested timeout"
        assert in_container_cmd[2:] == ["claude", "-p"], "original command must follow the timeout wrapper"

    @pytest.mark.asyncio
    async def test_exit_code_124_raises_dispatch_timeout_error(self):
        """coreutils timeout(1) exits 124 when the child is killed; must raise DispatchTimeoutError."""
        fake_proc = _make_fake_proc(returncode=124)

        async def fake_create(*args, **kwargs):
            return fake_proc

        with patch("asyncio.create_subprocess_exec", fake_create):
            with pytest.raises(DispatchTimeoutError):
                await _run_in_container("mycontainer", ["claude", "-p"], timeout=60)

    @pytest.mark.asyncio
    async def test_asyncio_timeout_kills_local_proc_and_raises_dispatch_timeout_error(self):
        """asyncio.TimeoutError must kill the local docker exec proc and surface as DispatchTimeoutError."""
        fake_proc = _make_fake_proc(returncode=0)

        async def fake_create(*args, **kwargs):
            return fake_proc

        async def raise_timeout(coro, timeout):
            # Close the coroutine so no unawaited-coroutine warning fires.
            coro.close()
            raise asyncio.TimeoutError()

        with patch("asyncio.create_subprocess_exec", fake_create):
            with patch("asyncio.wait_for", raise_timeout):
                with pytest.raises(DispatchTimeoutError):
                    await _run_in_container("mycontainer", ["claude", "-p"], timeout=60)

        fake_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_successful_run_not_affected(self):
        """Non-timeout runs must return stdout/stderr/returncode unchanged."""
        fake_proc = _make_fake_proc(returncode=0, stdout=b"hello", stderr=b"warn")

        async def fake_create(*args, **kwargs):
            return fake_proc

        with patch("asyncio.create_subprocess_exec", fake_create):
            stdout, stderr, rc = await _run_in_container("mycontainer", ["echo", "hi"], timeout=30)

        assert stdout == "hello"
        assert stderr == "warn"
        assert rc == 0
