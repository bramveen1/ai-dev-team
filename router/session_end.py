"""Router session end — handles clean exit and timeout exit flows.

Detects exit trigger phrases, extracts memory from conversations,
and manages the session shutdown process including memory persistence
and thread summary posting.
"""

import json
import logging
import re
from typing import Any

from router.dispatcher import _run_in_container
from router.memory_writer import persist_memory
from router.thread_loader import HARNESS_SUMMARY_EVENT_TYPE

logger = logging.getLogger(__name__)

# Exit trigger phrases (case-insensitive). A message is an exit trigger
# if it contains any of these phrases.
EXIT_TRIGGERS = [
    "thanks",
    "thank you",
    "cheers",
    "that's all",
    "thats all",
    "bye",
]

# Format for the thread summary posted on timeout
SUMMARY_FORMAT = (
    "_Session paused. Here's where we left off:_\n"
    "_Topic: {topic}_\n"
    "_Key points: {key_points}_\n"
    "_Open question: {open_question}_\n"
    "_Pending action: {pending_action}_"
)

# Posted when context capture fails (CLI returns nothing)
SUMMARY_CAPTURE_FAILED_MESSAGE = "_Session paused. Context could not be captured._"

# Number of recent message exchanges to include in the deterministic fallback summary
FALLBACK_SUMMARY_EXCHANGES = 6

# Max characters per message line in the fallback summary (including the "[user]: " prefix)
FALLBACK_SUMMARY_MAX_LINE_LENGTH = 200

# Prompt sent to CLI for memory extraction
MEMORY_EXTRACTION_PROMPT = (
    "Based on this conversation, extract any: decisions made, preferences expressed, "
    "people mentioned, project updates, and lessons learned. "
    "Return as JSON with keys: decisions (list of {{date, topic, content}}), "
    "preferences (list of {{date, content}}), people (list of {{name, context}}), "
    "projects (list of {{name, update}}), agent_memory (string summary), "
    "daily_log (string summary). Use today's date where applicable. "
    "If nothing notable, return empty lists/strings.\n\n"
    "Conversation:\n{conversation}"
)

# Prompt sent to CLI for thread summary generation
SUMMARY_EXTRACTION_PROMPT = (
    "Summarize this conversation in a compact format. Return JSON with keys: "
    "topic (what we were discussing), key_points (decisions and highlights), "
    "open_question (what was unresolved), pending_action (what was being waited on). "
    "Each value should be a short string.\n\n"
    "Conversation:\n{conversation}"
)


def is_exit_trigger(message: str) -> bool:
    """Return True only if the message is predominantly a farewell/sign-off phrase.

    Uses whole-word matching anchored to the end of the message so that
    messages like "thanks, can you also do X?" do not falsely trigger a
    session exit.  Trailing punctuation is stripped before matching so that
    "thanks!" and "thanks" are treated identically.

    Args:
        message: The message text to check.

    Returns:
        True if the message ends with a recognised exit trigger phrase.
    """
    if not message:
        return False

    # Strip trailing punctuation/whitespace so "thanks!" matches "thanks".
    normalized = message.lower().strip().rstrip("!. ")

    for trigger in EXIT_TRIGGERS:
        # \b enforces a whole-word boundary so "bye" does not match inside
        # "goodbye"; $ anchors to the end of the string so the trigger must
        # be the last substantive content of the message.
        if re.search(r"\b" + re.escape(trigger) + r"$", normalized):
            return True

    return False


def extract_memory(response: str) -> str:
    """Extract a memory block from a structured agent response.

    Looks for a "## Memory" section in the response and returns its content.

    Args:
        response: The agent's response text.

    Returns:
        Content of the memory block, or empty string if none found.
    """
    if not response:
        return ""

    match = re.search(r"## Memory\n(.*?)(?:\n## |\Z)", response, re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""


def _format_thread_for_prompt(thread_history: list[dict]) -> str:
    """Format thread history into a string for CLI prompts."""
    lines = []
    for msg in thread_history:
        user = msg.get("user", "unknown")
        text = msg.get("text", "")
        lines.append(f"[{user}]: {text}")
    return "\n".join(lines)


def _extract_json(text: str) -> dict[str, Any]:
    """Extract a JSON object from text that may contain markdown fences or preamble."""
    # Try direct parse first
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass

    # Try extracting from markdown code block (```json ... ``` or ``` ... ```)
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError):
            pass

    # Try finding first { ... } block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except (json.JSONDecodeError, TypeError):
            pass

    return {}


async def _invoke_cli_for_extraction(
    container: str,
    prompt: str,
    timeout: int = 60,
) -> dict:
    """Invoke Claude Code CLI with a prompt and parse JSON response.

    Args:
        container: Docker container name.
        prompt: The prompt to send.
        timeout: Timeout in seconds.

    Returns:
        Parsed JSON dict from the CLI response.
    """
    cli_cmd = [
        "claude",
        "--dangerously-skip-permissions",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--no-session-persistence",
        "--max-turns",
        "1",
    ]

    try:
        stdout, stderr, returncode = await _run_in_container(container, cli_cmd, timeout)
    except Exception as e:
        logger.warning("CLI extraction failed [exception]: %s", e)
        return {}

    if returncode != 0:
        logger.warning("CLI extraction failed [nonzero-exit code=%d]: %s", returncode, stderr[:200])
        return {}

    if not stdout.strip():
        logger.warning("CLI extraction failed [empty-stdout]")
        return {}

    # The CLI wraps output in {"result": "..."} when using --output-format json
    try:
        data = json.loads(stdout)
        result_text = data.get("result", "")
    except json.JSONDecodeError:
        logger.warning("CLI extraction failed [non-json-stdout]: %s", stdout[:200])
        result_text = stdout

    parsed = _extract_json(result_text)
    if not parsed:
        logger.warning("CLI extraction failed [unparseable-json]: %s", result_text[:300])
    return parsed


def _build_fallback_summary(thread_history: list[dict]) -> str:
    """Build a deterministic fallback summary directly from thread_history.

    Used when CLI-based extraction fails. Does not call the CLI, so it cannot
    itself fail or rate-limit. The returned string always begins with a
    SUMMARY_MARKERS-compatible prefix so thread_loader.split_messages_at_summary
    still finds a split boundary.

    Args:
        thread_history: Full thread history.

    Returns:
        A summary string containing real message content, or
        SUMMARY_CAPTURE_FAILED_MESSAGE when thread_history is empty.
    """
    if not thread_history:
        return SUMMARY_CAPTURE_FAILED_MESSAGE

    recent = thread_history[-FALLBACK_SUMMARY_EXCHANGES:]
    lines = []
    for msg in recent:
        user = msg.get("user", "unknown")
        text = msg.get("text", "")
        line = f"[{user}]: {text}"
        if len(line) > FALLBACK_SUMMARY_MAX_LINE_LENGTH:
            line = line[:FALLBACK_SUMMARY_MAX_LINE_LENGTH]
        lines.append(line)

    content = "\n".join(lines)
    return f"_Session paused. Context auto-captured from recent messages:_\n{content}"


async def handle_clean_exit(
    agent_name: str,
    container: str,
    thread_history: list[dict],
    slack_client,
    channel: str,
    thread_ts: str,
) -> int:
    """Handle a clean session exit triggered by a farewell message.

    Extracts memory from the conversation and persists it.

    Args:
        agent_name: Name of the agent.
        container: Docker container name for the agent.
        thread_history: Full thread history.
        slack_client: Slack WebClient for posting messages.
        channel: Slack channel ID.
        thread_ts: Slack thread timestamp.

    Returns:
        Number of memory items persisted.
    """
    conversation = _format_thread_for_prompt(thread_history)
    prompt = MEMORY_EXTRACTION_PROMPT.format(conversation=conversation)

    try:
        memory_data = await _invoke_cli_for_extraction(container, prompt)
        count = await persist_memory(agent_name, memory_data)
        logger.info("Session ended cleanly for %s, %d items persisted", agent_name, count)
        return count
    except Exception:
        logger.exception("Error during clean exit memory persistence for %s", agent_name)
        return 0


async def handle_timeout_exit(
    agent_name: str,
    container: str,
    thread_history: list[dict],
    slack_client,
    channel: str,
    thread_ts: str,
) -> int:
    """Handle a session timeout — persist memory and post thread summary.

    Same as clean exit, but also generates and posts a thread summary
    to Slack so the conversation can be resumed later.

    Args:
        agent_name: Name of the agent.
        container: Docker container name for the agent.
        thread_history: Full thread history.
        slack_client: Slack WebClient for posting messages.
        channel: Slack channel ID.
        thread_ts: Slack thread timestamp.

    Returns:
        Number of memory items persisted.
    """
    conversation = _format_thread_for_prompt(thread_history)
    count = 0

    # Generate and post thread summary
    try:
        summary_prompt = SUMMARY_EXTRACTION_PROMPT.format(conversation=conversation)
        summary_data = await _invoke_cli_for_extraction(container, summary_prompt)

        if not summary_data:
            summary_text = _build_fallback_summary(thread_history)
        else:
            summary_text = SUMMARY_FORMAT.format(
                topic=summary_data.get("topic", "Unknown"),
                key_points=summary_data.get("key_points", "None recorded"),
                open_question=summary_data.get("open_question", "None"),
                pending_action=summary_data.get("pending_action", "None"),
            )

        await slack_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=summary_text,
            metadata={
                "event_type": HARNESS_SUMMARY_EVENT_TYPE,
                "event_payload": {},
            },
        )
        logger.info("Posted timeout summary to channel=%s thread=%s", channel, thread_ts)
    except Exception:
        logger.exception("Error posting timeout summary for %s", agent_name)

    # Extract and persist memory (same as clean exit)
    try:
        memory_prompt = MEMORY_EXTRACTION_PROMPT.format(conversation=conversation)
        memory_data = await _invoke_cli_for_extraction(container, memory_prompt)
        count = await persist_memory(agent_name, memory_data)
        logger.info("Session timed out for %s, summary posted, %d items persisted", agent_name, count)
    except Exception:
        logger.exception("Error during timeout memory persistence for %s", agent_name)

    return count
