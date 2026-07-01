"""Discord transport adapter — implements ChatAdapter using discord.py.

One DiscordAdapter instance represents one bot application (one agent). For
multi-agent deployments (Sam + Lisa) the caller creates two DiscordAdapter
instances, each with its own bot token, and runs them via ``asyncio.gather``.

Feature flag: ``DISCORD_ENABLED`` (env var). Adapter instantiation succeeds
regardless of the flag; the flag is checked by the caller that wires the
adapter into the router. Defaults to ``False``.

Adapter-private encoding:
  - ``ConversationRef`` → ``"discord:<guild_id>:<channel_id>:<thread_id>"``
                          thread_id is "0" when the message is in the channel
                          root (not a thread).
  - ``PrincipalRef``    → ``"discord:<user_snowflake>"``

Core never sees these encodings. Only this file constructs/decodes refs.

Rate limiting: Discord allows 5 messages per 5 seconds per channel. This
adapter enforces that bound with a per-channel token-bucket. HTTP 429
responses from the REST API are retried with the ``retry_after`` backoff the
API returns. Gateway disconnects are handled by discord.py's built-in
auto-reconnect.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import warnings
from collections import defaultdict
from typing import Any

with warnings.catch_warnings():
    # discord.py transitively imports the stdlib ``audioop`` module, which emits
    # a DeprecationWarning on Python 3.11 (audioop is removed in 3.13). The repo's
    # pytest config runs warnings-as-errors, so this import must be shielded here —
    # co-located with the cause, without weakening global warning policy.
    warnings.simplefilter("ignore", DeprecationWarning)
    import discord

from router.chat.core import run_agent_turn
from router.chat.interface import ChatAdapter
from router.chat.types import (
    AdapterCapabilities,
    AdapterStatus,
    ConversationRef,
    InboundMessage,
    OutboundMessage,
    PrincipalRef,
    PromptChoice,
    StructuredResponse,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

DISCORD_ENABLED: bool = os.environ.get("DISCORD_ENABLED", "").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Rate-limit constants (Discord REST: 5 msg / 5 s per channel)
# ---------------------------------------------------------------------------

_RATE_LIMIT_MESSAGES = 5
_RATE_LIMIT_WINDOW = 5.0  # seconds

# ---------------------------------------------------------------------------
# Message-length limit (Discord REST: content must be <= 2000 chars,
# error code 50035). Longer responses are split across multiple messages;
# without this the whole turn 400s and the user sees nothing.
# ---------------------------------------------------------------------------

_MAX_MESSAGE_LEN = 2000


def _split_message(text: str, limit: int = _MAX_MESSAGE_LEN) -> list[str]:
    """Split ``text`` into chunks each at most ``limit`` characters.

    Breaks on newline boundaries so code blocks / paragraphs stay intact where
    possible; a single line longer than ``limit`` is hard-sliced. Never emits an
    empty chunk from non-empty input. Empty/short input returns ``[text]``
    unchanged so short messages still produce exactly one send.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        # A single line longer than the limit: flush pending, then hard-slice.
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        # +1 accounts for the "\n" that re-joins this line to the current chunk.
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        elif current:
            current = current + "\n" + line
        else:
            current = line
    if current:
        chunks.append(current)
    return chunks or [text]


# ---------------------------------------------------------------------------
# Status labels for non-reaction transports
# ---------------------------------------------------------------------------

_STATUS_LABELS: dict[AdapterStatus, str] = {
    AdapterStatus.THINKING: "...",
    AdapterStatus.WORKING: "⚙️",
    AdapterStatus.ERROR: "❌",
}

# ---------------------------------------------------------------------------
# Wire-format helpers (adapter-private; opaque to core)
# ---------------------------------------------------------------------------

_REF_PREFIX = "discord:"
_REF_SEP = ":"


def _encode_ref(guild_id: int | str, channel_id: int | str, thread_id: int | str = 0) -> ConversationRef:
    """Build a Discord ``ConversationRef``. Only called from within this adapter."""
    return ConversationRef(f"{_REF_PREFIX}{guild_id}{_REF_SEP}{channel_id}{_REF_SEP}{thread_id}")


def _decode_ref(ref: ConversationRef) -> tuple[int, int, int]:
    """Decode a Discord ``ConversationRef`` into (guild_id, channel_id, thread_id).

    thread_id of 0 means the message is in the channel root (not a thread).
    """
    body = ref.removeprefix(_REF_PREFIX)
    parts = body.split(_REF_SEP)
    if len(parts) != 3:  # noqa: PLR2004
        raise ValueError(f"Malformed Discord ConversationRef: {ref!r}")
    guild_id, channel_id, thread_id = parts
    return int(guild_id), int(channel_id), int(thread_id)


def _encode_principal(user_id: int | str) -> PrincipalRef:
    """Build a Discord ``PrincipalRef``. Only called from within this adapter."""
    return PrincipalRef(f"discord:{user_id}")


def _decode_principal(ref: PrincipalRef) -> int:
    """Decode a Discord ``PrincipalRef`` back to a snowflake integer."""
    return int(ref.removeprefix("discord:"))


# ---------------------------------------------------------------------------
# Per-channel token-bucket for rate limiting
# ---------------------------------------------------------------------------


class _ChannelBucket:
    """Token bucket enforcing Discord's 5-msg / 5-s per-channel rate limit."""

    def __init__(self, max_tokens: int = _RATE_LIMIT_MESSAGES, window: float = _RATE_LIMIT_WINDOW) -> None:
        self._max = max_tokens
        self._window = window
        self._timestamps: list[float] = []

    async def acquire(self) -> None:
        """Block until sending one more message is within the rate limit."""
        while True:
            now = time.monotonic()
            # Drop timestamps outside the current window.
            self._timestamps = [t for t in self._timestamps if now - t < self._window]
            if len(self._timestamps) < self._max:
                self._timestamps.append(now)
                return
            # Wait until the oldest token in the window expires.
            wait = self._window - (now - self._timestamps[0])
            logger.debug("DiscordAdapter rate-limit: sleeping %.2fs for channel bucket", wait)
            await asyncio.sleep(max(wait, 0))


# ---------------------------------------------------------------------------
# DiscordAdapter
# ---------------------------------------------------------------------------


class DiscordAdapter(ChatAdapter):
    """Discord transport adapter — one instance per bot / per agent.

    For multi-agent deployments, instantiate two adapters (one per bot token)
    and run both clients via ``asyncio.gather``.  The adapter is intentionally
    stateless with respect to agent identity: it knows only its own bot token
    and the agent name that owns it.
    """

    _CAPABILITIES = AdapterCapabilities(
        supports_threads=True,
        supports_channels=True,
        supports_interactive=True,
    )

    def __init__(
        self,
        bot_token: str,
        agent_name: str,
        *,
        default_channel_id: int = 0,
        client: discord.Client | None = None,
    ) -> None:
        """
        Args:
            bot_token:          Discord bot token for this agent's application.
            agent_name:         Logical agent name (``"sam"`` / ``"lisa"``); used
                                for mention parsing and logging.
            default_channel_id: Fallback channel ID for proactive / cron messages
                                with no ``conversation_ref``.
            client:             Injected ``discord.Client`` (useful for testing).
                                When ``None`` a real client is created with
                                ``message_content`` intent requested.
        """
        self._token = bot_token
        self._agent_name = agent_name.lower()
        self._default_channel_id = default_channel_id
        self._buckets: dict[int, _ChannelBucket] = defaultdict(_ChannelBucket)
        self._pending_choices: dict[str, asyncio.Future[StructuredResponse]] = {}

        if client is not None:
            self._client = client
            self._intent_guard_passed: bool | None = None
        else:
            intents = discord.Intents.default()
            intents.message_content = True
            self._client = discord.Client(intents=intents)
            self._intent_guard_passed = None

        self._setup_event_handlers()

    # ------------------------------------------------------------------
    # Capability declaration
    # ------------------------------------------------------------------

    @property
    def capabilities(self) -> AdapterCapabilities:
        return self._CAPABILITIES

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def send_message(self, outbound: OutboundMessage) -> None:
        """Send ``outbound.text`` to the Discord channel / thread.

        Falls back to ``default_channel_id`` when ``conversation_ref`` is
        ``None``.  Enforces per-channel rate limit before each send and retries
        once on HTTP 429 with the ``retry_after`` delay the API returns.
        """
        if outbound.conversation_ref is None:
            channel_id = self._default_channel_id
            thread_id = 0
        else:
            _, channel_id, thread_id = _decode_ref(outbound.conversation_ref)

        channel = self._client.get_channel(channel_id)
        if channel is None:
            logger.warning("DiscordAdapter.send_message: channel %s not in cache, skipping", channel_id)
            return

        target: Any = channel
        if thread_id:
            thread = channel.get_thread(thread_id)
            if thread is not None:
                target = thread

        # Discord caps content at 2000 chars; split long turns into multiple
        # messages, each rate-limited and 429-retried independently.
        for chunk in _split_message(outbound.text):
            await self._buckets[channel_id].acquire()
            try:
                await target.send(chunk)
            except discord.HTTPException as exc:
                if exc.status == 429:  # Too Many Requests
                    retry_after = getattr(exc, "retry_after", 1.0)
                    logger.warning("DiscordAdapter: HTTP 429, retrying after %.1fs", retry_after)
                    await asyncio.sleep(retry_after)
                    await target.send(chunk)
                else:
                    logger.error("DiscordAdapter.send_message HTTP %s: %s", exc.status, exc.text)
                    raise

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def read_thread(self, conversation_ref: ConversationRef) -> list[InboundMessage]:
        """Fetch the message history for the channel / thread identified by ``conversation_ref``.

        Returns messages in chronological order. Uses the REST API (``history``
        endpoint) so recent messages not yet in the gateway cache are included.
        """
        _, channel_id, thread_id = _decode_ref(conversation_ref)

        channel = self._client.get_channel(channel_id)
        if channel is None:
            logger.warning("DiscordAdapter.read_thread: channel %s not in cache", channel_id)
            return []

        target: Any = channel
        if thread_id:
            thread = channel.get_thread(thread_id)
            if thread is not None:
                target = thread

        messages: list[InboundMessage] = []
        try:
            async for msg in target.history(limit=100, oldest_first=True):
                ref = make_inbound_ref(
                    guild_id=msg.guild.id if msg.guild else 0,
                    channel_id=channel_id,
                    thread_id=thread_id,
                )
                principal = _encode_principal(msg.author.id)
                messages.append(
                    InboundMessage(
                        conversation_ref=ref,
                        principal_ref=principal,
                        text=msg.content,
                    )
                )
        except discord.HTTPException as exc:
            logger.error("DiscordAdapter.read_thread HTTP %s: %s", exc.status, exc.text)

        return messages

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def set_status(self, conversation_ref: ConversationRef, state: AdapterStatus) -> None:
        """Send a short status indicator to the conversation.

        Discord has no native typing-indicator API that stays up indefinitely, so
        this sends a brief text status string instead.  ``AdapterStatus.DONE``
        and ``AdapterStatus.ERROR`` are the only states that produce visible
        output; ``THINKING`` and ``WORKING`` trigger a Discord typing indicator.
        """
        _, channel_id, thread_id = _decode_ref(conversation_ref)
        channel = self._client.get_channel(channel_id)
        if channel is None:
            return

        target: Any = channel
        if thread_id:
            thread = channel.get_thread(thread_id)
            if thread is not None:
                target = thread

        if state in (AdapterStatus.THINKING, AdapterStatus.WORKING):
            try:
                async with target.typing():
                    pass
            except discord.HTTPException:
                pass
        elif state == AdapterStatus.DONE:
            return
        else:
            label = _STATUS_LABELS.get(state, state.value)
            try:
                await target.send(label)
            except discord.HTTPException as exc:
                logger.debug("DiscordAdapter.set_status send failed: %s", exc)

    # ------------------------------------------------------------------
    # Identity resolution
    # ------------------------------------------------------------------

    def resolve_principal(self, raw_user_id: str) -> PrincipalRef:
        """Wrap a Discord snowflake string as a ``PrincipalRef``.

        The ``discord:`` prefix makes Discord refs structurally distinct from
        Slack UIDs (``U…``) and terminal refs (``local:…``).
        """
        return _encode_principal(raw_user_id)

    # ------------------------------------------------------------------
    # Mention parsing
    # ------------------------------------------------------------------

    def parse_mentions(self, text: str, conversation_ref: ConversationRef) -> list[str]:
        """Extract agent-name mentions from ``text``.

        Discord mentions are ``<@SNOWFLAKE>``; this adapter also recognises
        plain ``@agent_name`` mentions as used in bot commands. The adapter
        resolves snowflakes to logical agent names using its own bot-user map.

        Returns logical agent names in order of first appearance, deduplicated.
        """
        seen: set[str] = set()
        names: list[str] = []

        # Discord snowflake mentions (<@123456789012345678>) — resolved via
        # the client's user cache.  Unknown snowflakes are skipped; only
        # display-name matches against known agent names are returned.
        # Must be processed before the plain @name pass to avoid double-counting.
        snowflake_spans: set[int] = set()
        for m in re.finditer(r"<@!?(\d+)>", text):
            for i in range(m.start(), m.end()):
                snowflake_spans.add(i)
            snowflake = m.group(1)
            user = self._client.get_user(int(snowflake))
            if user is not None:
                key = user.display_name.lower()
                if key not in seen:
                    seen.add(key)
                    names.append(key)

        # Plain @name mentions (e.g. "@sam" / "@lisa"), excluding positions
        # already consumed by a snowflake mention above.
        for m in re.finditer(r"@(\w+)", text):
            if m.start() in snowflake_spans:
                continue
            key = m.group(1).lower()
            if key not in seen:
                seen.add(key)
                names.append(key)

        return names

    # ------------------------------------------------------------------
    # Interactive primitive
    # ------------------------------------------------------------------

    async def prompt_for_choice(
        self,
        conversation_ref: ConversationRef,
        prompt: PromptChoice,
    ) -> StructuredResponse:
        """Post an interactive choice card and await the user's button press.

        Renders the ``ApprovalCard`` as a Discord message with action-row
        buttons.  The response is delivered via the ``on_interaction`` handler
        and resolved through a stored ``asyncio.Future``.

        The pending state is stored in ``self._pending_choices`` (keyed by
        draft_id / correlation key) so the approval round-trip survives beyond
        the 15-minute Discord interaction-token TTL — button presses send a new
        interaction rather than editing the original.
        """
        import uuid

        correlation_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[StructuredResponse] = loop.create_future()
        self._pending_choices[correlation_id] = future

        # Build a Discord View with one button per choice.
        view = _ChoiceView(choices=prompt.choices, correlation_id=correlation_id, adapter=self)

        _, channel_id, thread_id = _decode_ref(conversation_ref)
        channel = self._client.get_channel(channel_id)
        if channel is None:
            del self._pending_choices[correlation_id]
            return StructuredResponse(choice=prompt.choices[0], index=0)

        target: Any = channel
        if thread_id:
            thread = channel.get_thread(thread_id)
            if thread is not None:
                target = thread

        await self._buckets[channel_id].acquire()
        try:
            await target.send(prompt.prompt, view=view)
        except discord.HTTPException as exc:
            logger.error("DiscordAdapter.prompt_for_choice send failed: %s", exc)
            del self._pending_choices[correlation_id]
            return StructuredResponse(choice=prompt.choices[0], index=0)

        try:
            return await asyncio.wait_for(future, timeout=prompt.timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning("DiscordAdapter.prompt_for_choice timed out after %ss", prompt.timeout_seconds)
            self._pending_choices.pop(correlation_id, None)
            return StructuredResponse(choice=prompt.choices[0], index=0)

    def _resolve_choice(self, correlation_id: str, response: StructuredResponse) -> None:
        """Resolve a pending :meth:`prompt_for_choice` future from an interaction handler."""
        future = self._pending_choices.pop(correlation_id, None)
        if future is not None and not future.done():
            future.set_result(response)

    # ------------------------------------------------------------------
    # Gateway lifecycle & intent guard
    # ------------------------------------------------------------------

    def _setup_event_handlers(self) -> None:
        """Register discord.py event handlers on the client."""

        @self._client.event
        async def on_ready() -> None:
            logger.info("DiscordAdapter[%s] connected as %s", self._agent_name, self._client.user)
            self._intent_guard_passed = True

        @self._client.event
        async def on_message(message: discord.Message) -> None:
            if message.author == self._client.user:
                return
            if not message.content and self._intent_guard_passed is not False:
                logger.error(
                    "DiscordAdapter[%s]: received message with empty content — "
                    "MESSAGE CONTENT intent may be disabled.  "
                    "Enable it at https://discord.com/developers/applications → Bot → Privileged Gateway Intents.",
                    self._agent_name,
                )
                self._intent_guard_passed = False

            # Derive thread context: when the message arrives inside a Thread, use its id and parent channel.
            is_thread = isinstance(message.channel, discord.Thread)
            if is_thread:
                thread_id: int = message.channel.id
                channel_id: int = message.channel.parent_id
            else:
                thread_id = 0
                channel_id = message.channel.id

            # Gate: process when @-mentioned OR bot is already a member of this thread (follow-up without re-mention).
            mentioned = self._client.user in message.mentions
            in_followed_thread = is_thread and message.channel.me is not None
            if not mentioned and not in_followed_thread:
                return

            if not message.content:
                return

            if message.guild is None:
                return

            # Root-channel mention: open a new thread (or reuse one already attached to this message).
            if thread_id == 0:
                existing_thread = getattr(message, "thread", None)
                if existing_thread is not None:
                    thread_id = existing_thread.id
                else:
                    try:
                        thread_name = message.content[:80] or "conversation"
                        thread = await message.create_thread(name=thread_name)
                        await thread.join()
                        thread_id = thread.id
                    except discord.HTTPException as exc:
                        logger.warning(
                            "DiscordAdapter[%s]: could not open thread (%s), falling back to flat channel reply",
                            self._agent_name,
                            exc,
                        )

            conversation_ref = make_inbound_ref(message.guild.id, channel_id, thread_id)
            principal_ref = self.resolve_principal(str(message.author.id))
            inbound = InboundMessage(
                conversation_ref=conversation_ref,
                principal_ref=principal_ref,
                text=message.content,
            )

            try:
                await run_agent_turn(self, inbound, agent_name=self._agent_name)
            except Exception:
                logger.exception("DiscordAdapter[%s]: error dispatching inbound message", self._agent_name)
                try:
                    await self.set_status(conversation_ref, AdapterStatus.ERROR)
                    await self.send_message(
                        OutboundMessage(text="hit an error, check the logs", conversation_ref=conversation_ref)
                    )
                except Exception:
                    logger.error(
                        "DiscordAdapter[%s]: failed to post error notification", self._agent_name, exc_info=True
                    )

    async def start(self) -> None:
        """Start the Discord gateway connection (blocks until disconnect)."""
        await self._client.start(self._token)

    async def close(self) -> None:
        """Gracefully close the gateway connection."""
        await self._client.close()


# ---------------------------------------------------------------------------
# Discord UI helpers (View + Buttons for prompt_for_choice)
# ---------------------------------------------------------------------------


class _ChoiceButton(discord.ui.Button):
    """A single choice button inside a ``_ChoiceView``."""

    def __init__(self, label: str, index: int, correlation_id: str, adapter: DiscordAdapter) -> None:
        super().__init__(label=label, custom_id=f"{correlation_id}:{index}")
        self._index = index
        self._correlation_id = correlation_id
        self._adapter = adapter

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        response = StructuredResponse(choice=self.label, index=self._index)
        self._adapter._resolve_choice(self._correlation_id, response)
        self.view.stop()


class _ChoiceView(discord.ui.View):
    """Discord UI View that renders one button per choice option."""

    def __init__(self, choices: list[str], correlation_id: str, adapter: DiscordAdapter) -> None:
        super().__init__(timeout=None)
        for i, label in enumerate(choices):
            self.add_item(_ChoiceButton(label=label, index=i, correlation_id=correlation_id, adapter=adapter))


# ---------------------------------------------------------------------------
# Adapter-internal helper — the only place ConversationRef is constructed
# for a Discord event. Core calls this indirectly via the inbound path.
# ---------------------------------------------------------------------------


def make_inbound_ref(guild_id: int | str, channel_id: int | str, thread_id: int | str = 0) -> ConversationRef:
    """Construct a ``ConversationRef`` from a Discord message event.

    This is the single construction point for Discord refs. Core never calls
    this directly — it only sees the ref after the adapter creates it.

    The ``discord:`` prefix makes Discord refs structurally distinct from Slack
    ``channel:thread`` refs and terminal ``terminal:session`` refs.
    """
    return _encode_ref(guild_id, channel_id, thread_id)
