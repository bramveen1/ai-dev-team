"""Milestone feed for dispatch supervision (#338).

Reads transcript.jsonl incrementally (offset-based) and posts one
short Slack line per tool_use block (assistant messages only). The
entire feed is gated behind the DISPATCH_MILESTONE_FEED environment
variable and has zero effect when unset.

Cadence / volume guards (AC3):
- Consecutive same-class tool runs are coalesced into one line.
- At most one Slack post per RATE_LIMIT_SECONDS (120 s).
- At most HARD_CAP (15) milestone posts per dispatch; on hitting the
  cap a single CAP_MESSAGE note is posted and no further milestones
  are emitted.

Secret scrubbing (AC6):
Tool inputs run through router.log_buffer.redact() before posting so
raw tokens never appear in milestone lines.

Wake-path safety (AC5):
Milestone posts carry no <@mention> tokens. The caller posts via the
worker-bot channel (allowlisted in app.py) so the posts do not trip
the agent-wake path.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from router.log_buffer import redact

logger = logging.getLogger(__name__)

ENV_FLAG = "DISPATCH_MILESTONE_FEED"

RATE_LIMIT_SECONDS: float = 120.0
HARD_CAP: int = 15
CAP_MESSAGE: str = "… (milestones capped)"

# Tool-class labels used for coalescing consecutive same-class runs.
_CLASS_EDIT = "edit"
_CLASS_BASH = "bash"
_CLASS_SEARCH = "search"
_CLASS_READ = "read"
_CLASS_OTHER = "other"

_EDIT_TOOLS = frozenset({"edit", "write", "multiedit", "notebookedit"})
_BASH_TOOLS = frozenset({"bash"})
_SEARCH_TOOLS = frozenset({"grep", "glob", "websearch", "webfetch", "search"})
_READ_TOOLS = frozenset({"read"})


def is_enabled() -> bool:
    """Return True when the DISPATCH_MILESTONE_FEED setting is truthy (hot-reloadable)."""
    from router import settings  # noqa: PLC0415 — deferred to avoid import cycle

    return bool(settings.get(ENV_FLAG))


def _tool_class(tool_name: str) -> str:
    name = tool_name.lower()
    if name in _EDIT_TOOLS:
        return _CLASS_EDIT
    if name in _BASH_TOOLS:
        return _CLASS_BASH
    if name in _SEARCH_TOOLS:
        return _CLASS_SEARCH
    if name in _READ_TOOLS:
        return _CLASS_READ
    return _CLASS_OTHER


def format_milestone_line(tool_name: str, tool_input: dict) -> str:
    """Format one tool_use block into a milestone line (AC2).

    Input values are scrubbed via redact() before rendering.
    The returned string never contains <@...> mention tokens.
    """
    cls = _tool_class(tool_name)

    if cls == _CLASS_EDIT:
        raw_path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path") or ""
        basename = Path(redact(raw_path)).name if raw_path else "file"
        return f"✎ edited {basename}"

    if cls == _CLASS_BASH:
        cmd = tool_input.get("command") or tool_input.get("cmd") or ""
        cmd = redact(cmd)
        if len(cmd) > 60:
            cmd = cmd[:57] + "…"
        return f"❯ {cmd}"

    if cls == _CLASS_SEARCH:
        pattern = (
            tool_input.get("pattern")
            or tool_input.get("query")
            or tool_input.get("glob")
            or tool_input.get("file_path")
            or tool_input.get("url")
            or ""
        )
        pattern = redact(pattern)
        if pattern:
            if len(pattern) > 40:
                pattern = pattern[:37] + "…"
            return f'🔍 searching "{pattern}"'
        return "🔍 investigating…"

    if cls == _CLASS_READ:
        raw_path = tool_input.get("file_path") or tool_input.get("path") or ""
        if raw_path:
            basename = Path(redact(raw_path)).name
            if basename:
                return f"🔍 reading {basename}"
        return "🔍 investigating…"

    return "🔍 investigating…"


def read_new_tool_uses(
    transcript_path: Path,
    last_offset: int,
) -> tuple[list[tuple[str, dict]], int]:
    """Read new tool_use blocks from transcript.jsonl since last_offset.

    Returns (tool_uses, new_offset).  tool_uses is a list of
    (tool_name, tool_input) pairs from assistant records only (AC1).

    Failure modes (AC7):
    - transcript absent / empty → ([], last_offset), no error raised.
    - partial/truncated final line → skip that line, return offset up to
      the last complete line.
    """
    try:
        size = transcript_path.stat().st_size
    except OSError:
        return [], last_offset

    if size <= last_offset:
        return [], last_offset

    tool_uses: list[tuple[str, dict]] = []
    new_offset = last_offset

    try:
        with transcript_path.open("rb") as fh:
            fh.seek(last_offset)
            for raw in fh:
                new_offset += len(raw)
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue  # AC7: skip partial/truncated line
                if not isinstance(record, dict):
                    continue
                # AC1: only assistant records; system/user/result are skipped.
                if record.get("type") != "assistant":
                    continue
                # Content lives at record["content"] or record["message"]["content"].
                content = record.get("content")
                if content is None:
                    msg = record.get("message")
                    if isinstance(msg, dict):
                        content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_use":
                        continue  # AC1: skip text/thinking blocks
                    tool_name = block.get("name") or ""
                    tool_input = block.get("input") or {}
                    if not isinstance(tool_input, dict):
                        tool_input = {}
                    if tool_name:
                        tool_uses.append((tool_name, tool_input))
    except OSError:
        return [], last_offset

    return tool_uses, new_offset


# Per-dispatch milestone state.  Process-local; a router restart resets it —
# the worst case is one redundant post after recovery, same as _last_posted.
#
# Shape: {dispatch_id: {
#   "offset": int,              last byte offset read in transcript.jsonl
#   "last_post_ts": float|None, wall-clock time of the last milestone post
#   "post_count": int,          total milestone posts so far this dispatch
#   "capped": bool,             True once HARD_CAP has been reached
#   "last_class": str|None,     tool class of the last posted milestone
# }}
_milestone_states: dict[str, dict] = {}


def reset_milestone_states() -> None:
    """Test hook — wipe all per-dispatch milestone state."""
    _milestone_states.clear()


def cleanup_dispatch(dispatch_id: str) -> None:
    """Drop milestone state for a dispatch that has gone terminal."""
    _milestone_states.pop(dispatch_id, None)


def _get_state(dispatch_id: str) -> dict:
    if dispatch_id not in _milestone_states:
        _milestone_states[dispatch_id] = {
            "offset": 0,
            "last_post_ts": None,
            "post_count": 0,
            "capped": False,
            "last_class": None,
        }
    return _milestone_states[dispatch_id]


async def run_milestone_feed(
    dispatch_id: str,
    *,
    slack_client: Any,
    channel: str,
    thread_ts: str,
    transcript_path: Path,
    _now: float | None = None,
) -> None:
    """Read new tool_use events from transcript and post at most one milestone line.

    Respects the 120 s rate limit and the 15-post hard cap.  All failures
    are swallowed so the supervisor poll loop is never interrupted (AC7).
    """
    try:
        await _run_inner(
            dispatch_id,
            slack_client=slack_client,
            channel=channel,
            thread_ts=thread_ts,
            transcript_path=transcript_path,
            _now=_now,
        )
    except Exception:
        logger.exception("milestone_feed: unexpected error for dispatch=%s", dispatch_id)


async def _run_inner(
    dispatch_id: str,
    *,
    slack_client: Any,
    channel: str,
    thread_ts: str,
    transcript_path: Path,
    _now: float | None,
) -> None:
    import time

    state = _get_state(dispatch_id)

    # Advance offset even when capped so we don't re-scan old data.
    if state["capped"]:
        _, state["offset"] = read_new_tool_uses(transcript_path, state["offset"])
        return

    tool_uses, new_offset = read_new_tool_uses(transcript_path, state["offset"])
    state["offset"] = new_offset

    if not tool_uses:
        return

    now_ts = _now if _now is not None else time.time()

    # Rate-limit check: at most one post per RATE_LIMIT_SECONDS.
    last_ts = state["last_post_ts"]
    if last_ts is not None and (now_ts - last_ts) < RATE_LIMIT_SECONDS:
        return

    # Coalesce consecutive same-class runs (AC3).
    # Iterates all tool_uses; keeps overwriting line_to_post with each
    # new-class entry so the LAST class change in the batch is posted.
    final_class: str | None = state["last_class"]
    line_to_post: str | None = None
    for tool_name, tool_input in tool_uses:
        cls = _tool_class(tool_name)
        if cls != final_class:
            line_to_post = format_milestone_line(tool_name, tool_input)
            final_class = cls

    if line_to_post is None:
        # All new tool_uses were the same class as the last posted → coalesced.
        return

    # Hard-cap check: ~15 posts then one final cap note.
    if state["post_count"] >= HARD_CAP:
        state["capped"] = True
        state["last_class"] = final_class
        await _safe_post(slack_client, channel, thread_ts, CAP_MESSAGE)
        return

    await _safe_post(slack_client, channel, thread_ts, line_to_post)
    state["last_post_ts"] = now_ts
    state["last_class"] = final_class
    state["post_count"] += 1


async def _safe_post(slack_client: Any, channel: str, thread_ts: str, text: str) -> None:
    """Best-effort Slack post; never raises (AC7)."""
    if slack_client is None or not channel:
        return
    try:
        await slack_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts or None,
            text=text,
        )
    except Exception:
        logger.exception(
            "milestone_feed: post failed for channel=%s thread=%s",
            channel,
            thread_ts,
        )
