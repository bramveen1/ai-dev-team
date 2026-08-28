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
import collections
import logging
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

from router import attachments as attachments_mod
from router import settings as _settings
from router.attachments import (
    attachments_enabled,
    ingest_files,
    validate_files,
)
from router.chat import inbound_common
from router.chat.core import InboundFacts, handle_inbound
from router.chat.input_collect import collect_input_scripted
from router.chat.interface import ChatAdapter
from router.chat.types import (
    AdapterCapabilities,
    AdapterStatus,
    ConversationRef,
    InboundMessage,
    InputRequest,
    InputResponse,
    OutboundMessage,
    PrincipalRef,
    PromptChoice,
    StructuredResponse,
)
from router.config import resolve_session_timeout
from router.thread_loader import SUMMARY_MARKERS
from router.threads.state import get_default_store

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

DISCORD_ENABLED: bool = bool(_settings.get("DISCORD_ENABLED"))

# ---------------------------------------------------------------------------
# Bot-message allowlist (parity with Slack's DISPATCH_BOT_USER_IDS)
#
# Bot-authored messages are dropped before they can trigger an agent turn —
# same loop protection the Slack path applies. Snowflakes listed in this env
# var (comma-separated) are allowed through, mirroring the dispatch-bot
# allowlist that powers the auto-review handoff on Slack.
# ---------------------------------------------------------------------------

DISPATCH_BOT_IDS_ENV = "DISCORD_DISPATCH_BOT_IDS"


def _allowlisted_bot_ids() -> set[str]:
    """Snowflakes of bot users allowed to trigger agent turns (read per call, hot-reloadable)."""
    raw = _settings.get(DISPATCH_BOT_IDS_ENV)
    return {part.strip() for part in raw.split(",") if part.strip()}


# ---------------------------------------------------------------------------
# Event deduplication (parity with app.py's _seen_events)
#
# The gateway can redeliver a message event after a reconnect/resume. Each
# adapter remembers the message IDs it has started processing and drops
# duplicates within the TTL window. The algorithm is shared with the Slack
# path via router.chat.inbound_common; the store stays per-adapter.
# ---------------------------------------------------------------------------

_SEEN_EVENTS_MAX: int = inbound_common.SEEN_EVENTS_MAX
_SEEN_EVENTS_TTL: float = inbound_common.SEEN_EVENTS_TTL

# ---------------------------------------------------------------------------
# Attachments (parity with app.py's file-ingest pipeline, #328)
# ---------------------------------------------------------------------------

_ATTACHMENTS_ROOT = attachments_mod._ATTACHMENTS_ROOT

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
# Typing keepalive constants
# ---------------------------------------------------------------------------

# Cadence for re-sending the typing indicator. Discord expires it after ~10s so
# 8s gives comfortable headroom even under light network jitter.
_TYPING_KEEPALIVE_INTERVAL: float = 8.0

# How many extra seconds to keep the backstop alive beyond the session timeout.
# If set_status(DONE/ERROR) never arrives (crashed worker), the task self-heals.
_TYPING_KEEPALIVE_MAX_EXTRA: int = 30

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


def is_discord_ref(value: str) -> bool:
    """Structural check: does *value* look like a Discord-encoded ref?

    Lets non-adapter router code (e.g. ``internal_api``, ``approvals/execute``)
    route Discord-origin drafts by the shape of the stored ``conversation_id``
    instead of comparing a raw ``transport`` string (#553) — the encoding
    itself stays private to this module.
    """
    return bool(value) and value.startswith(_REF_PREFIX)


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
        tasks_store: Any = None,
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
            tasks_store:        Optional
                                :class:`~router.scheduled_tasks.store.ScheduledTaskStore`
                                for ``aidt tasks list`` support over Discord.
        """
        self._token = bot_token
        self._agent_name = agent_name.lower()
        self._default_channel_id = default_channel_id
        self._tasks_store = tasks_store
        self._buckets: dict[int, _ChannelBucket] = defaultdict(_ChannelBucket)
        self._pending_choices: dict[str, asyncio.Future[StructuredResponse]] = {}
        # message-id → expiry epoch; FIFO-evicted at _SEEN_EVENTS_MAX.
        self._seen_events: collections.OrderedDict[Any, float] = collections.OrderedDict()
        # Per-conversation typing keepalive tasks (keyed by ConversationRef).
        self._typing_keepalives: dict[ConversationRef, asyncio.Task] = {}
        # Extra on_interaction listeners fanned out by _setup_event_handlers
        # (discord.Client has no add_listener — that's a commands.Bot API).
        self._interaction_handlers: list[Any] = []

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

    @property
    def agent_name(self) -> str:
        """Logical agent name this adapter's bot speaks for."""
        return self._agent_name

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    def can_post(self, conversation_ref: ConversationRef) -> bool:
        """Cheap membership/postability precheck for *conversation_ref* (#707).

        ``True`` only when the target channel (and thread, if any) is present
        in this bot's gateway cache — i.e. it is a guild member and has seen
        the channel/thread. Used to turn a would-be 403 into a structured
        degrade before ``send_message`` is attempted.
        """
        _, channel_id, thread_id = _decode_ref(conversation_ref)
        channel = self._client.get_channel(channel_id)
        if channel is None:
            return False
        if thread_id:
            return channel.get_thread(thread_id) is not None
        return True

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
                # Guard 2 (#547): only summaries this bot itself posted are
                # provenance-verified session boundaries. Marker text from any
                # other author must never collapse the thread.
                is_summary = bool(
                    msg.author == self._client.user and any(marker in msg.content for marker in SUMMARY_MARKERS)
                )
                messages.append(
                    InboundMessage(
                        conversation_ref=ref,
                        principal_ref=principal,
                        text=msg.content,
                        is_summary=is_summary,
                    )
                )
        except discord.HTTPException as exc:
            logger.error("DiscordAdapter.read_thread HTTP %s: %s", exc.status, exc.text)

        return messages

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _cancel_typing_keepalive(self, conversation_ref: ConversationRef) -> None:
        """Cancel and discard any running typing keepalive for ``conversation_ref``."""
        task = self._typing_keepalives.pop(conversation_ref, None)
        if task is not None and not task.done():
            task.cancel()

    async def _typing_keepalive_loop(self, conversation_ref: ConversationRef, max_lifetime: float) -> None:
        """Re-send the typing indicator on a loop until cancelled or max_lifetime expires.

        Fires immediately on first iteration, then sleeps _TYPING_KEEPALIVE_INTERVAL
        seconds between subsequent sends. max_lifetime is a hard backstop so a
        missing terminal status (crashed worker) doesn't type forever.
        HTTP errors are swallowed so a transient failure doesn't kill the task.
        """
        _, channel_id, thread_id = _decode_ref(conversation_ref)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_lifetime
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.debug("DiscordAdapter: typing keepalive max-lifetime reached for %s", conversation_ref)
                return
            channel = self._client.get_channel(channel_id)
            if channel is not None:
                target: Any = channel
                if thread_id:
                    t = channel.get_thread(thread_id)
                    if t is not None:
                        target = t
                try:
                    async with target.typing():
                        pass
                except discord.HTTPException:
                    pass
            await asyncio.sleep(min(_TYPING_KEEPALIVE_INTERVAL, remaining))

    async def set_status(self, conversation_ref: ConversationRef, state: AdapterStatus) -> None:
        """Send a short status indicator to the conversation.

        For THINKING / WORKING, a per-conversation background keepalive task is
        started that re-sends the typing indicator every ~8 s, keeping the bubble
        alive for the full duration of the agent turn.  The task is cancelled when
        DONE or ERROR arrives, or self-terminates via a max-lifetime backstop if
        no terminal status is delivered (e.g. a crashed worker).
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
            self._cancel_typing_keepalive(conversation_ref)
            max_lifetime = float(resolve_session_timeout() + _TYPING_KEEPALIVE_MAX_EXTRA)
            task = asyncio.create_task(
                self._typing_keepalive_loop(conversation_ref, max_lifetime),
                name=f"typing-keepalive-{conversation_ref}",
            )
            self._typing_keepalives[conversation_ref] = task
        elif state == AdapterStatus.DONE:
            self._cancel_typing_keepalive(conversation_ref)
        else:
            self._cancel_typing_keepalive(conversation_ref)
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

    async def collect_input(
        self,
        conversation_ref: ConversationRef,
        request: InputRequest,
    ) -> InputResponse:
        """Not yet implemented as a native Discord modal — reserved shape.

        ``supports_forms`` is ``False`` for this adapter, so callers should
        drive :func:`~router.chat.input_collect.collect_input_scripted`
        directly instead of calling this method. It is implemented here (via
        the same scripted helper) purely so the class stays concrete; a
        native ``discord.ui.Modal`` implementation is tracked separately.
        """
        return await collect_input_scripted(self, conversation_ref, request)

    def _resolve_choice(self, correlation_id: str, response: StructuredResponse) -> None:
        """Resolve a pending :meth:`prompt_for_choice` future from an interaction handler."""
        future = self._pending_choices.pop(correlation_id, None)
        if future is not None and not future.done():
            future.set_result(response)

    def register_interaction_handler(self, handler: Any) -> None:
        """Register an extra ``on_interaction`` listener (e.g. approval button clicks, #682).

        Handlers are appended to an adapter-owned list and fanned out by the
        single ``on_interaction`` event registered in
        :meth:`_setup_event_handlers`, so multiple registrations compose
        instead of clobbering each other. ``discord.Client`` has no
        ``add_listener`` (that is a ``commands.Bot`` API), and the event-based
        dispatch does not interfere with discord.py's internal View dispatch
        (which drives ``_ChoiceButton.callback`` for
        :meth:`prompt_for_choice`) — every registered handler runs for each
        ``INTERACTION_CREATE`` event, including component clicks on messages
        posted via a raw REST call rather than through this adapter's live
        client.
        """
        self._interaction_handlers.append(handler)

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
            await self._handle_inbound(message)

        @self._client.event
        async def on_interaction(interaction: discord.Interaction) -> None:
            for handler in list(self._interaction_handlers):
                try:
                    await handler(interaction)
                except Exception:
                    logger.exception(
                        "DiscordAdapter[%s]: interaction handler %r failed",
                        self._agent_name,
                        getattr(handler, "__name__", handler),
                    )

    # ------------------------------------------------------------------
    # Inbound pipeline (parity with app.py's _handle_event / handle_message)
    # ------------------------------------------------------------------

    def _is_duplicate_event(self, message_id: Any) -> bool:
        """Dedup guard keyed by message ID with TTL + FIFO cap (parity with app.py)."""
        return inbound_common.seen_recently(
            self._seen_events, message_id, ttl=_SEEN_EVENTS_TTL, max_size=_SEEN_EVENTS_MAX
        )

    async def _handle_aidt_command(self, message: discord.Message, conversation_ref: ConversationRef) -> bool:
        """Intercept ``@Agent aidt <verb> <args>`` messages (issue #658).

        Returns True when the message was an aidt command (handled, including
        error replies) — the caller must not fall through to an agent turn.
        The principal kind is set from the Discord ``author.bot`` flag so the
        human-only gate in core fires correctly.
        """
        from router.commands.core import dispatch_command
        from router.commands.discord_shim import parse_from_discord_message
        from router.commands.types import Principal

        principal_ref = self.resolve_principal(str(message.author.id))
        principal_kind = "bot" if message.author.bot else "human"
        principal = Principal(ref=str(principal_ref), kind=principal_kind)
        aidt_cmd = parse_from_discord_message(
            message.content,
            conversation_ref=str(conversation_ref),
            principal_ref=str(principal_ref),
        )
        if aidt_cmd is None:
            return False

        try:
            result = await dispatch_command(
                aidt_cmd,
                principal,
                subject_agent=self._agent_name,
                tasks_store=self._tasks_store,
            )
            await self.send_message(OutboundMessage(text=result.text, conversation_ref=conversation_ref))
        except Exception:
            logger.exception("DiscordAdapter[%s]: error dispatching aidt command", self._agent_name)
            try:
                await self.send_message(
                    OutboundMessage(
                        text="hit an error processing the command, check the logs",
                        conversation_ref=conversation_ref,
                    )
                )
            except Exception:
                logger.error(
                    "DiscordAdapter[%s]: failed to post aidt error notification",
                    self._agent_name,
                    exc_info=True,
                )
        return True

    async def _ingest_attachments(
        self,
        message: discord.Message,
        thread_key: str,
        conversation_ref: ConversationRef,
    ) -> tuple[list[str], bool]:
        """Download message attachments into the shared attachments store (#328 parity).

        Returns ``(paths, ok)``. ``ok`` is False when the dispatch must be
        aborted (validation rejection or ingest failure) — a user-facing reply
        has already been posted in that case, mirroring the Slack path.
        """
        raw_files = [
            {
                "id": str(att.id),
                "name": att.filename,
                "size": att.size,
                "mimetype": att.content_type or "",
                # Discord CDN URLs are pre-authorised; ingest downloads them
                # directly (the bearer token the Slack path needs is unused).
                "url_private": att.url,
            }
            for att in message.attachments
        ]
        if not raw_files:
            return [], True

        async def _post_attachment_notice(notice: str) -> None:
            await self.send_message(OutboundMessage(text=notice, conversation_ref=conversation_ref))

        # Shared validate→reject→ingest→warn→abort contract (same fail-clean
        # semantics as the Slack path) lives in inbound_common; validate/
        # ingest are passed from this module's namespace so existing test
        # patch targets keep intercepting. require_token=False: Discord CDN
        # URLs are pre-authorised, so ingest runs tokenless.
        return await inbound_common.ingest_attachments(
            raw_files,
            thread_key,
            "",
            attachments_root=_ATTACHMENTS_ROOT,
            agent_name=self._agent_name,
            post_notice=_post_attachment_notice,
            validate_fn=validate_files,
            ingest_fn=ingest_files,
            require_token=False,
        )

    async def _handle_inbound(self, message: discord.Message) -> None:  # noqa: PLR0911, PLR0912, PLR0915
        """Full inbound pipeline for one gateway message.

        Mirrors the legacy Slack path (``app._handle_event`` + routing rules in
        ``app.handle_message``) stage for stage: gating, bot-message guard,
        dedup, thread-state recording, pack commands, session management, exit
        trigger, memory curation, attachments, dispatch, handoff detection,
        and classified error replies.
        """
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

        # Derive conversation topology. DMs (no guild) are always handled,
        # like Slack's channel_type == "im" branch; guild messages need a
        # mention or thread-follow gate.
        is_dm = message.guild is None
        is_thread = isinstance(message.channel, discord.Thread)
        if is_thread:
            thread_id: int = message.channel.id
            channel_id: int = message.channel.parent_id
        else:
            thread_id = 0
            channel_id = message.channel.id

        mentioned = self._client.user in message.mentions
        if not is_dm and not mentioned:
            # Un-mentioned guild message: only thread follow-ups are eligible,
            # and only when this agent is the thread's active agent (parity
            # with app.handle_message routing). When no active agent has been
            # recorded yet, fall back to thread membership — this bot joined
            # the thread when it was engaged, so membership implies ownership.
            if not (is_thread and message.channel.me is not None):
                return
            try:
                active_agent = get_default_store().get_active_agent(str(channel_id), str(thread_id))
            except Exception:
                logger.exception("Failed to read thread state")
                active_agent = None
            if active_agent is not None and active_agent != self._agent_name:
                logger.info(
                    "routing.dropped reason=not_owned channel=%s thread=%s agent=%s",
                    channel_id,
                    thread_id,
                    self._agent_name,
                )
                return

        if not message.content:
            return

        # Deduplicate gateway redeliveries (reconnect/resume) by message ID.
        if self._is_duplicate_event(message.id):
            logger.debug("dedup: dropping duplicate message id=%s agent=%s", message.id, self._agent_name)
            return

        # Root-channel mention: open a new thread (or reuse one already attached to this message).
        if not is_dm and thread_id == 0:
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

        guild_id = message.guild.id if message.guild else 0
        conversation_ref = make_inbound_ref(guild_id, channel_id, thread_id)
        principal_ref = self.resolve_principal(str(message.author.id))
        channel_key = str(channel_id)
        # Slack keys per-thread state by thread_ts (the root message ts when no
        # thread exists yet); the Discord analogue is the thread id, falling
        # back to the message id for flat-channel/DM messages.
        thread_key = str(thread_id) if thread_id else str(message.id)

        # aidt command surface runs before the bot-message guard on purpose:
        # dispatch_command carries its own human-only gate and replies with a
        # rejection for bot principals (#658).
        if await self._handle_aidt_command(message, conversation_ref):
            return

        # Bot-message guard (parity with app.py): drop bot-authored messages
        # unless allowlisted, so two agents can never echo-loop each other.
        author_is_bot = bool(message.author.bot)
        if author_is_bot:
            if str(message.author.id) not in _allowlisted_bot_ids():
                logger.debug("Ignoring bot message from %s", message.author.id)
                return
            # Guard 1 (#547): peer/harness summaries are context only — they
            # must never create a dispatchable turn.
            if any(marker in message.content for marker in SUMMARY_MARKERS):
                logger.info(
                    "guard1: skipping dispatch for peer/harness summary agent=%s text=%.80s",
                    self._agent_name,
                    message.content,
                )
                return

        # All remaining stages are transport-neutral — hand the decoded facts
        # to the shared orchestrator (chat/core.handle_inbound). The ingest
        # callback closes over the gateway message so attachment decoding and
        # notice posting stay adapter-local (and this module's validate/ingest
        # test seams keep working).
        ingest = None
        if attachments_enabled() and message.attachments:

            async def ingest() -> tuple[list[str], bool]:
                return await self._ingest_attachments(message, thread_key, conversation_ref)

        facts = InboundFacts(
            agent_name=self._agent_name,
            conversation_ref=conversation_ref,
            principal_ref=principal_ref,
            channel_key=channel_key,
            thread_key=thread_key,
            text=message.content,
            author_id=str(message.author.id),
            author_is_bot=author_is_bot,
            mentioned=mentioned,
            transport="discord",
            attachments_root=_ATTACHMENTS_ROOT,
        )
        await handle_inbound(self, facts, ingest=ingest)

    async def start(self) -> None:
        """Start the Discord gateway connection (blocks until disconnect)."""
        await self._client.start(self._token)

    async def close(self) -> None:
        """Gracefully close the gateway connection."""
        for task in list(self._typing_keepalives.values()):
            if not task.done():
                task.cancel()
        self._typing_keepalives.clear()
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
