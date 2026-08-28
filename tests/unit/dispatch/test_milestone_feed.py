"""Unit tests for router.dispatch.milestone_feed (#338).

Covers:
- AC1: only assistant tool_use blocks emit milestones (system/user/
  assistant-text/result are skipped).
- AC2: correct line shapes per tool class.
- AC3: coalescing + rate-limit + hard-cap behaviour.
- AC4: flag-off (DISPATCH_MILESTONE_FEED unset) → zero posts via
  supervision.check_dispatch.
- AC5: no <@...> mention in any milestone line.
- AC6: secret scrubbing (PAT / token strings are redacted).
- AC7: graceful failure modes (empty/absent transcript, partial JSON).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from router.dispatch import feed_transport, milestone_feed

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_START = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
_T0 = _START.timestamp()


def _make_slack_client() -> MagicMock:
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True})
    return client


def _assistant_tool_use(name: str, **input_kwargs) -> str:
    """Return a transcript.jsonl line for an assistant message with one tool_use."""
    record = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": name,
                    "input": input_kwargs,
                }
            ],
        },
    }
    return json.dumps(record)


def _assistant_text(text: str = "hello") -> str:
    record = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }
    return json.dumps(record)


def _system_line() -> str:
    return json.dumps({"type": "system", "subtype": "init"})


def _user_line() -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}})


def _result_line() -> str:
    return json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.01})


def _write_transcript(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def reset_state():
    milestone_feed.reset_milestone_states()
    yield
    milestone_feed.reset_milestone_states()


# ---------------------------------------------------------------------------
# AC1: Source record filtering
# ---------------------------------------------------------------------------


class TestSourceRecordFiltering:
    """Only assistant tool_use blocks produce tool_uses; others are skipped."""

    def test_system_record_skipped(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        _write_transcript(path, [_system_line()])
        result, offset = milestone_feed.read_new_tool_uses(path, 0)
        assert result == []
        assert offset > 0

    def test_user_record_skipped(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        _write_transcript(path, [_user_line()])
        result, _ = milestone_feed.read_new_tool_uses(path, 0)
        assert result == []

    def test_result_record_skipped(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        _write_transcript(path, [_result_line()])
        result, _ = milestone_feed.read_new_tool_uses(path, 0)
        assert result == []

    def test_assistant_text_block_skipped(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        _write_transcript(path, [_assistant_text()])
        result, _ = milestone_feed.read_new_tool_uses(path, 0)
        assert result == []

    def test_assistant_tool_use_extracted(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        _write_transcript(path, [_assistant_tool_use("Bash", command="ls")])
        result, _ = milestone_feed.read_new_tool_uses(path, 0)
        assert len(result) == 1
        assert result[0] == ("Bash", {"command": "ls"})

    def test_mixed_transcript_only_tool_use_extracted(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        lines = [
            _system_line(),
            _user_line(),
            _assistant_text(),
            _assistant_tool_use("Edit", file_path="/src/foo.py", old_string="x", new_string="y"),
            _assistant_tool_use("Bash", command="pytest"),
            _result_line(),
        ]
        _write_transcript(path, lines)
        result, _ = milestone_feed.read_new_tool_uses(path, 0)
        assert len(result) == 2
        assert result[0][0] == "Edit"
        assert result[1][0] == "Bash"

    def test_flat_content_format_also_works(self, tmp_path):
        """Some CLI versions emit content at record level (not nested in message)."""
        record = {
            "type": "assistant",
            "content": [{"type": "tool_use", "name": "Read", "id": "t1", "input": {"file_path": "/a/b.py"}}],
        }
        path = tmp_path / "transcript.jsonl"
        _write_transcript(path, [json.dumps(record)])
        result, _ = milestone_feed.read_new_tool_uses(path, 0)
        assert len(result) == 1
        assert result[0][0] == "Read"


# ---------------------------------------------------------------------------
# AC2: Line shape per tool class
# ---------------------------------------------------------------------------


class TestLineShapes:
    def test_edit_tool_formats_basename(self):
        line = milestone_feed.format_milestone_line("Edit", {"file_path": "/src/router/app.py"})
        assert line == "✎ edited app.py"

    def test_write_tool_formats_basename(self):
        line = milestone_feed.format_milestone_line("Write", {"file_path": "/tmp/out.txt"})
        assert line == "✎ edited out.txt"

    def test_notebookedit_formats_basename(self):
        line = milestone_feed.format_milestone_line("NotebookEdit", {"notebook_path": "/work/nb.ipynb"})
        assert line == "✎ edited nb.ipynb"

    def test_bash_formats_command(self):
        line = milestone_feed.format_milestone_line("Bash", {"command": "pytest tests/"})
        assert line == "❯ pytest tests/"

    def test_bash_truncates_at_60_chars(self):
        cmd = "x" * 70
        line = milestone_feed.format_milestone_line("Bash", {"command": cmd})
        assert len(line) <= len("❯ ") + 60
        assert line.endswith("…")

    def test_bash_exact_60_chars_not_truncated(self):
        cmd = "a" * 60
        line = milestone_feed.format_milestone_line("Bash", {"command": cmd})
        assert "…" not in line
        assert line == f"❯ {cmd}"

    def test_grep_formats_pattern(self):
        line = milestone_feed.format_milestone_line("Grep", {"pattern": "def foo"})
        assert line == '🔍 searching "def foo"'

    def test_glob_formats_pattern(self):
        line = milestone_feed.format_milestone_line("Glob", {"pattern": "**/*.py"})
        assert line == '🔍 searching "**/*.py"'

    def test_search_truncates_at_40_chars(self):
        pat = "z" * 50
        line = milestone_feed.format_milestone_line("Grep", {"pattern": pat})
        assert "…" in line
        inner = line.replace('🔍 searching "', "").rstrip('"')
        assert len(inner) <= 40

    def test_search_bare_when_no_pattern(self):
        line = milestone_feed.format_milestone_line("Grep", {})
        assert line == "🔍 investigating…"

    def test_read_formats_basename(self):
        line = milestone_feed.format_milestone_line("Read", {"file_path": "/router/app.py"})
        assert line == "🔍 reading app.py"

    def test_read_bare_when_no_path(self):
        line = milestone_feed.format_milestone_line("Read", {})
        assert line == "🔍 investigating…"

    def test_unknown_tool_is_investigating(self):
        line = milestone_feed.format_milestone_line("Agent", {})
        assert line == "🔍 investigating…"

    def test_websearch_uses_query_field(self):
        line = milestone_feed.format_milestone_line("WebSearch", {"query": "Python asyncio"})
        assert line == '🔍 searching "Python asyncio"'


# ---------------------------------------------------------------------------
# AC3: Coalescing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCoalescing:
    async def test_consecutive_same_class_runs_emit_one_line(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        lines = [
            _assistant_tool_use("Bash", command="ls"),
            _assistant_tool_use("Bash", command="pwd"),
            _assistant_tool_use("Bash", command="whoami"),
        ]
        _write_transcript(path, lines)

        client = _make_slack_client()
        await milestone_feed.run_milestone_feed(
            "d1",
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            transcript_path=path,
            _now=_T0,
        )

        client.chat_postMessage.assert_awaited_once()

    async def test_class_change_emits_new_line(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        lines = [
            _assistant_tool_use("Bash", command="ls"),
            _assistant_tool_use("Edit", file_path="/foo.py"),
        ]
        _write_transcript(path, lines)

        client = _make_slack_client()
        await milestone_feed.run_milestone_feed(
            "d1",
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            transcript_path=path,
            _now=_T0,
        )

        # Last class change was Edit → only that one posts.
        client.chat_postMessage.assert_awaited_once()
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "✎ edited" in text

    async def test_coalescing_persists_across_ticks(self, tmp_path):
        """If last posted was 'bash', a second bash-only tick is coalesced away."""
        path = tmp_path / "transcript.jsonl"
        # First tick: bash
        _write_transcript(path, [_assistant_tool_use("Bash", command="ls")])
        client = _make_slack_client()
        await milestone_feed.run_milestone_feed(
            "d1",
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            transcript_path=path,
            _now=_T0,
        )
        assert client.chat_postMessage.await_count == 1

        # Second tick (past rate limit): more bash
        with path.open("a") as f:
            f.write(_assistant_tool_use("Bash", command="pwd") + "\n")
        client.chat_postMessage.reset_mock()
        await milestone_feed.run_milestone_feed(
            "d1",
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            transcript_path=path,
            _now=_T0 + 130,
        )
        client.chat_postMessage.assert_not_awaited()


# ---------------------------------------------------------------------------
# AC3: Rate-limit (≤1/120s)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRateLimit:
    async def test_second_call_within_120s_suppressed(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        _write_transcript(path, [_assistant_tool_use("Bash", command="ls")])

        client = _make_slack_client()
        await milestone_feed.run_milestone_feed(
            "d1",
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            transcript_path=path,
            _now=_T0,
        )
        assert client.chat_postMessage.await_count == 1

        # Add new tool_use; call within 120 s window → suppressed.
        with path.open("a") as f:
            f.write(_assistant_tool_use("Edit", file_path="/b.py") + "\n")
        client.chat_postMessage.reset_mock()
        await milestone_feed.run_milestone_feed(
            "d1",
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            transcript_path=path,
            _now=_T0 + 60,  # only 60 s later
        )
        client.chat_postMessage.assert_not_awaited()

    async def test_call_after_120s_allowed(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        _write_transcript(path, [_assistant_tool_use("Bash", command="ls")])

        client = _make_slack_client()
        await milestone_feed.run_milestone_feed(
            "d1",
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            transcript_path=path,
            _now=_T0,
        )
        assert client.chat_postMessage.await_count == 1

        with path.open("a") as f:
            f.write(_assistant_tool_use("Edit", file_path="/b.py") + "\n")
        client.chat_postMessage.reset_mock()
        await milestone_feed.run_milestone_feed(
            "d1",
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            transcript_path=path,
            _now=_T0 + 121,  # just past 120 s
        )
        client.chat_postMessage.assert_awaited_once()


# ---------------------------------------------------------------------------
# Regression: rate-limited ticks must not drop tool_uses / must persist
# last_class on the normal post path (#380)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestOffsetNotLostOnRateLimit:
    async def test_rate_limited_tick_does_not_drop_tool_use(self, tmp_path):
        """A class transition that arrives during a rate-limited tick must
        still be posted once the rate limit clears, instead of being
        silently lost because the read offset was committed too early."""
        path = tmp_path / "transcript.jsonl"
        _write_transcript(path, [_assistant_tool_use("Bash", command="ls")])
        client = _make_slack_client()
        await milestone_feed.run_milestone_feed(
            "d1",
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            transcript_path=path,
            _now=_T0,
        )
        assert client.chat_postMessage.await_count == 1

        # New tool_use (class change) arrives while still rate-limited.
        with path.open("a") as f:
            f.write(_assistant_tool_use("Edit", file_path="/b.py") + "\n")
        client.chat_postMessage.reset_mock()
        await milestone_feed.run_milestone_feed(
            "d1",
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            transcript_path=path,
            _now=_T0 + 10,  # within RATE_LIMIT_SECONDS
        )
        client.chat_postMessage.assert_not_awaited()

        # Once the rate limit clears, the earlier Edit must still surface.
        await milestone_feed.run_milestone_feed(
            "d1",
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            transcript_path=path,
            _now=_T0 + 130,
        )
        client.chat_postMessage.assert_awaited_once()
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "✎ edited b.py" in text

    async def test_last_class_persists_after_normal_post(self, tmp_path):
        """state['last_class'] must advance on the normal (non-cap) post
        path, mirroring the hard-cap path, so later coalescing decisions
        compare against the truly-last-posted class."""
        path = tmp_path / "transcript.jsonl"
        _write_transcript(path, [_assistant_tool_use("Edit", file_path="/a.py")])
        client = _make_slack_client()
        await milestone_feed.run_milestone_feed(
            "d1",
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            transcript_path=path,
            _now=_T0,
        )
        client.chat_postMessage.assert_awaited_once()
        state = milestone_feed._get_state("d1")
        assert state["last_class"] == milestone_feed._CLASS_EDIT


# ---------------------------------------------------------------------------
# AC3: Hard cap (~15 + capped note)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestHardCap:
    async def _post_n_milestones(self, tmp_path: Path, n: int) -> tuple[MagicMock, Path]:
        path = tmp_path / "transcript.jsonl"
        path.touch()
        client = _make_slack_client()
        classes = ["Bash", "Edit", "Read", "Grep", "Bash"]  # cycle
        for i in range(n):
            tool = classes[i % len(classes)]
            with path.open("a") as f:
                if tool == "Bash":
                    f.write(_assistant_tool_use("Bash", command=f"cmd{i}") + "\n")
                elif tool == "Edit":
                    f.write(_assistant_tool_use("Edit", file_path=f"/f{i}.py") + "\n")
                elif tool == "Read":
                    f.write(_assistant_tool_use("Read", file_path=f"/r{i}.py") + "\n")
                else:
                    f.write(_assistant_tool_use("Grep", pattern=f"pat{i}") + "\n")
            # Each iteration: different class guarantees no coalescing; 130 s apart.
            milestone_feed.reset_milestone_states()
            prev_state = milestone_feed._get_state("d1")
            prev_state["post_count"] = i
            prev_state["last_class"] = None  # force every entry to post
            prev_state["last_post_ts"] = _T0 + (i - 1) * 130
            await milestone_feed.run_milestone_feed(
                "d1",
                slack_client=client,
                channel="C1",
                thread_ts="1.0",
                transcript_path=path,
                _now=_T0 + i * 130,
            )
        return client, path

    async def test_hard_cap_posts_cap_message_at_15(self, tmp_path):
        """After 15 regular posts the next eligible run emits CAP_MESSAGE."""
        path = tmp_path / "transcript.jsonl"
        path.touch()
        client = _make_slack_client()

        # Seed state: 15 posts already made, not capped yet.
        state = milestone_feed._get_state("d1")
        state["post_count"] = 15
        state["last_post_ts"] = _T0 - 200  # well past rate limit
        state["last_class"] = None

        with path.open("a") as f:
            f.write(_assistant_tool_use("Edit", file_path="/new.py") + "\n")

        await milestone_feed.run_milestone_feed(
            "d1",
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            transcript_path=path,
            _now=_T0,
        )

        client.chat_postMessage.assert_awaited_once()
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert text == milestone_feed.CAP_MESSAGE

    async def test_no_further_posts_after_cap(self, tmp_path):
        """Once capped, subsequent ticks produce zero posts."""
        path = tmp_path / "transcript.jsonl"
        path.touch()
        client = _make_slack_client()

        state = milestone_feed._get_state("d1")
        state["post_count"] = 15
        state["last_post_ts"] = _T0 - 200
        state["last_class"] = None

        # First call: triggers cap note
        with path.open("a") as f:
            f.write(_assistant_tool_use("Bash", command="ls") + "\n")
        await milestone_feed.run_milestone_feed(
            "d1",
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            transcript_path=path,
            _now=_T0,
        )
        assert client.chat_postMessage.await_count == 1

        # Second call: capped → zero additional posts
        client.chat_postMessage.reset_mock()
        with path.open("a") as f:
            f.write(_assistant_tool_use("Edit", file_path="/z.py") + "\n")
        await milestone_feed.run_milestone_feed(
            "d1",
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            transcript_path=path,
            _now=_T0 + 130,
        )
        client.chat_postMessage.assert_not_awaited()


# ---------------------------------------------------------------------------
# AC5: No <@mention> in milestone posts
# ---------------------------------------------------------------------------


class TestNoMentionTokens:
    def test_all_format_functions_produce_no_mention(self):
        cases = [
            ("Edit", {"file_path": "/a/b.py"}),
            ("Write", {"file_path": "/c.py"}),
            ("Bash", {"command": "git push"}),
            ("Grep", {"pattern": "foo"}),
            ("Glob", {"pattern": "*.py"}),
            ("Read", {"file_path": "/d.py"}),
            ("Agent", {}),
        ]
        for tool_name, tool_input in cases:
            line = milestone_feed.format_milestone_line(tool_name, tool_input)
            assert "<@" not in line, f"mention found in '{line}' for tool '{tool_name}'"

    @pytest.mark.asyncio
    async def test_posted_text_contains_no_mention(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        _write_transcript(path, [_assistant_tool_use("Bash", command="ls")])
        client = _make_slack_client()
        await milestone_feed.run_milestone_feed(
            "d1",
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            transcript_path=path,
            _now=_T0,
        )
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "<@" not in text


# ---------------------------------------------------------------------------
# AC6: Secret scrubbing
# ---------------------------------------------------------------------------


class TestSecretScrubbing:
    def test_bash_command_with_token_is_redacted(self):
        cmd = "git clone https://x-access-token:ghp_SECRETTOKEN123@github.com/org/repo"
        line = milestone_feed.format_milestone_line("Bash", {"command": cmd})
        assert "ghp_SECRETTOKEN123" not in line
        assert "REDACTED" in line or "[REDACTED" in line

    def test_file_path_with_pat_in_basename_redacted(self):
        # If the path itself contains a token (>= 10 chars after prefix),
        # the redact pass strips it before the basename is extracted.
        token = "ghp_SECRETPATTOKEN12345"
        line = milestone_feed.format_milestone_line("Edit", {"file_path": f"/work/{token}.py"})
        assert token not in line


# ---------------------------------------------------------------------------
# AC7: Graceful failure modes
# ---------------------------------------------------------------------------


class TestGracefulFailureModes:
    def test_absent_transcript_returns_empty(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        result, offset = milestone_feed.read_new_tool_uses(path, 0)
        assert result == []
        assert offset == 0

    def test_empty_transcript_returns_empty(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        path.touch()
        result, offset = milestone_feed.read_new_tool_uses(path, 0)
        assert result == []
        assert offset == 0

    def test_partial_json_line_skipped(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        valid = _assistant_tool_use("Bash", command="ls")
        # Write valid line, then a truncated/corrupt line.
        path.write_text(valid + "\n{bad json\n", encoding="utf-8")
        result, _ = milestone_feed.read_new_tool_uses(path, 0)
        assert len(result) == 1
        assert result[0][0] == "Bash"

    @pytest.mark.asyncio
    async def test_slack_error_does_not_raise(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        _write_transcript(path, [_assistant_tool_use("Bash", command="ls")])
        client = _make_slack_client()
        client.chat_postMessage.side_effect = RuntimeError("slack down")
        # Must not raise.
        await milestone_feed.run_milestone_feed(
            "d1",
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            transcript_path=path,
            _now=_T0,
        )

    @pytest.mark.asyncio
    async def test_no_channel_skips_post_gracefully(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        _write_transcript(path, [_assistant_tool_use("Bash", command="ls")])
        client = _make_slack_client()
        await milestone_feed.run_milestone_feed(
            "d1",
            slack_client=client,
            channel="",
            thread_ts="1.0",
            transcript_path=path,
            _now=_T0,
        )
        client.chat_postMessage.assert_not_awaited()


# ---------------------------------------------------------------------------
# AC4: Feature flag — flag-off produces zero posts via check_dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestFeatureFlag:
    async def test_flag_off_zero_posts_from_check_dispatch(self, tmp_path, monkeypatch):
        """When DISPATCH_MILESTONE_FEED is off, check_dispatch posts nothing extra.

        The registry default is on, so "off" is an explicit =0 (via .env or the
        /config page), not merely an unset variable.
        """
        monkeypatch.setenv(milestone_feed.ENV_FLAG, "0")

        from router.dispatch import state as dstate
        from router.dispatch import supervision

        supervision.reset_delta_cache()
        root = str(tmp_path)

        # Seed a running dispatch.
        dstate.write_field("d1", dstate.FIELD_STARTED_AT, _START.isoformat(), root=root)
        dstate.write_field("d1", dstate.FIELD_BUDGET, "3600", root=root)
        dstate.write_field("d1", dstate.FIELD_CHANNEL, "C1", root=root)
        dstate.write_field("d1", dstate.FIELD_THREAD_TS, "1.0", root=root)
        dstate.write_field("d1", dstate.FIELD_AGENT, "sam", root=root)
        hb = tmp_path / "d1" / dstate.FIELD_HEARTBEAT
        hb.touch()

        # Write a transcript with tool_use events.
        transcript = tmp_path / "d1" / "transcript.jsonl"
        _write_transcript(transcript, [_assistant_tool_use("Bash", command="ls")])

        client = _make_slack_client()
        result = await supervision.check_dispatch(
            payload={"dispatch_id": "d1", "channel": "C1", "thread_ts": "1.0", "agent": "sam"},
            slack_client=client,
            now=_START,
            dispatch_root=root,
        )

        assert result == {"status": "ok"}
        client.chat_postMessage.assert_not_awaited()

    async def test_flag_on_posts_milestone_from_check_dispatch(self, tmp_path, monkeypatch):
        """When DISPATCH_MILESTONE_FEED=1, check_dispatch posts a milestone line."""
        monkeypatch.setenv(milestone_feed.ENV_FLAG, "1")

        from router.dispatch import state as dstate
        from router.dispatch import supervision

        supervision.reset_delta_cache()
        root = str(tmp_path)

        dstate.write_field("d2", dstate.FIELD_STARTED_AT, _START.isoformat(), root=root)
        dstate.write_field("d2", dstate.FIELD_BUDGET, "3600", root=root)
        dstate.write_field("d2", dstate.FIELD_CHANNEL, "C2", root=root)
        dstate.write_field("d2", dstate.FIELD_THREAD_TS, "2.0", root=root)
        dstate.write_field("d2", dstate.FIELD_AGENT, "sam", root=root)
        hb = tmp_path / "d2" / dstate.FIELD_HEARTBEAT
        hb.touch()

        transcript = tmp_path / "d2" / "transcript.jsonl"
        _write_transcript(transcript, [_assistant_tool_use("Edit", file_path="/work/app.py")])

        client = _make_slack_client()
        await supervision.check_dispatch(
            payload={"dispatch_id": "d2", "channel": "C2", "thread_ts": "2.0", "agent": "sam"},
            slack_client=client,
            now=_START,
            dispatch_root=root,
        )

        client.chat_postMessage.assert_awaited_once()
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "✎ edited app.py" in text
        assert "<@" not in text


# ---------------------------------------------------------------------------
# Incremental offset reading
# ---------------------------------------------------------------------------


class TestIncrementalOffsetReading:
    def test_offset_advances_correctly(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        line1 = _assistant_tool_use("Bash", command="ls")
        line2 = _assistant_tool_use("Edit", file_path="/b.py")
        path.write_text(line1 + "\n", encoding="utf-8")

        result1, offset1 = milestone_feed.read_new_tool_uses(path, 0)
        assert len(result1) == 1

        # Append second line.
        with path.open("a") as f:
            f.write(line2 + "\n")

        result2, offset2 = milestone_feed.read_new_tool_uses(path, offset1)
        assert len(result2) == 1
        assert result2[0][0] == "Edit"
        assert offset2 > offset1

    def test_no_new_data_returns_same_offset(self, tmp_path):
        path = tmp_path / "transcript.jsonl"
        _write_transcript(path, [_assistant_tool_use("Bash", command="ls")])
        _, offset1 = milestone_feed.read_new_tool_uses(path, 0)
        result2, offset2 = milestone_feed.read_new_tool_uses(path, offset1)
        assert result2 == []
        assert offset2 == offset1


# ---------------------------------------------------------------------------
# #713: ChatAdapter routing (flag-gated)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestChatAdapterRouting:
    """Milestone lines route through a ChatAdapter when
    DISPATCH_FEED_VIA_CHAT_ADAPTER is on and the dispatch carries a
    resolvable non-Slack transport ref (#713)."""

    async def test_flag_off_discord_ref_posts_nothing(self, tmp_path, monkeypatch):
        monkeypatch.delenv(feed_transport.ENV_FLAG, raising=False)
        path = tmp_path / "transcript.jsonl"
        _write_transcript(path, [_assistant_tool_use("Bash", command="ls")])

        client = _make_slack_client()
        await milestone_feed.run_milestone_feed(
            "d1",
            slack_client=client,
            channel="",
            thread_ts="",
            transcript_path=path,
            agent="sam",
            transport="discord",
            conversation_id="discord:1:2:3",
            _now=_T0,
        )
        client.chat_postMessage.assert_not_awaited()

    async def test_flag_on_discord_ref_posts_via_adapter(self, tmp_path, monkeypatch):
        monkeypatch.setenv(feed_transport.ENV_FLAG, "1")
        adapter = MagicMock()
        adapter.send_message = AsyncMock()
        monkeypatch.setattr(feed_transport.runtime, "discord_adapter_for_agent", lambda agent: adapter)

        path = tmp_path / "transcript.jsonl"
        _write_transcript(path, [_assistant_tool_use("Edit", file_path="/work/app.py")])

        client = _make_slack_client()
        await milestone_feed.run_milestone_feed(
            "d1",
            slack_client=client,
            channel="",
            thread_ts="",
            transcript_path=path,
            agent="sam",
            transport="discord",
            conversation_id="discord:1:2:3",
            _now=_T0,
        )

        client.chat_postMessage.assert_not_awaited()
        adapter.send_message.assert_awaited_once()
        outbound = adapter.send_message.await_args.args[0]
        assert str(outbound.conversation_ref) == "discord:1:2:3"
        assert "✎ edited app.py" in outbound.text

    async def test_flag_on_slack_ref_unaffected(self, tmp_path, monkeypatch):
        """Flag on with no transport (the default/Slack case) — zero effect."""
        monkeypatch.setenv(feed_transport.ENV_FLAG, "1")
        path = tmp_path / "transcript.jsonl"
        _write_transcript(path, [_assistant_tool_use("Bash", command="ls")])

        client = _make_slack_client()
        await milestone_feed.run_milestone_feed(
            "d1",
            slack_client=client,
            channel="C1",
            thread_ts="1.0",
            transcript_path=path,
            _now=_T0,
        )
        client.chat_postMessage.assert_awaited_once()
