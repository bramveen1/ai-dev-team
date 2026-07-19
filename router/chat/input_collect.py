"""Transport-neutral scripted-Q&A fulfilment for :class:`~router.chat.types.InputRequest`.

Every adapter, regardless of ``AdapterCapabilities.supports_forms``, can
fulfil an ``InputRequest`` by driving it as a sequence of question/answer
messages over the two primitives every :class:`~router.chat.interface.ChatAdapter`
already implements: :meth:`~router.chat.interface.ChatAdapter.send_message`
and :meth:`~router.chat.interface.ChatAdapter.read_thread`. This module is
that fallback — it talks only to ``ChatAdapter``; no ``slack_sdk`` import
below this module. Transports whose inbound messages arrive as pushed
events rather than pollable history supply ``await_reply`` (backed by
:mod:`router.chat.pending_input`) instead of the ``read_thread`` poll.

Rich transports with native form support (the Slack modal in
``router.chat.adapters.slack_forms``) fulfil ``InputRequest`` through
:meth:`~router.chat.interface.ChatAdapter.collect_input` instead; this
helper is what runs when that capability is absent.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Awaitable, Callable

from router.chat.types import (
    ConversationRef,
    InboundMessage,
    InputField,
    InputFieldType,
    InputRequest,
    InputResponse,
    OutboundMessage,
)

if TYPE_CHECKING:
    from router.chat.interface import ChatAdapter

DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_MAX_ATTEMPTS = 3

# Pluggable reply source for transports that push inbound messages as events
# instead of exposing a pollable history (see ``router.chat.pending_input``).
# Called with the request's timeout; returns the user's next message text, or
# ``None`` when no reply arrived in time.
AwaitReply = Callable[[float], Awaitable[str | None]]

_CANCEL_KEYWORD = "cancel"
_SECRET_VISIBILITY_WARNING = (
    ":warning: This transport has no secure input form — your reply will be visible in the channel history."
)


async def collect_input_scripted(
    adapter: "ChatAdapter",
    conversation_ref: ConversationRef,
    request: InputRequest,
    *,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    await_reply: AwaitReply | None = None,
) -> InputResponse:
    """Fulfil ``request`` by posting one prompt per field and awaiting a reply.

    For each field: post a prompt (re-posted with the validation error on a
    failed attempt) → take the next inbound message on ``conversation_ref``
    as the raw value → validate → on success move to the next field, on
    failure retry up to ``max_attempts`` times. Typing ``cancel`` at any
    prompt ends the form early with ``status="cancelled"``. Exhausting
    attempts is treated the same as an explicit cancel — the caller decides
    whether to re-ask. A reply that never arrives within
    ``request.timeout_seconds`` ends the form with ``status="timed_out"``.

    Args:
        adapter: Any ``ChatAdapter`` — only ``send_message``/``read_thread``
            are used, so this works identically across transports.
        conversation_ref: Where to post prompts and read replies from.
        request: The form to fulfil.
        poll_interval_seconds: Delay between ``read_thread`` polls while
            waiting for the next reply.
        max_attempts: Validation attempts allowed per field before giving up.
        await_reply: Optional push-based reply source. When set, replies come
            from this callable instead of polling ``read_thread`` — used by
            transports whose inbound messages arrive as events (e.g. the
            Slack adapter feeding :mod:`router.chat.pending_input`).

    Returns:
        An :class:`~router.chat.types.InputResponse` with whatever values
        were collected before completion, cancellation, or timeout.
    """
    values: dict[str, str] = {}
    # ``seen`` is the count of thread messages that precede the reply we are
    # currently waiting for. Real transports echo the bot's own posts into the
    # same history ``read_thread`` returns, so EVERY message we send — the
    # title and each prompt/reprompt — has to advance ``seen``; otherwise
    # ``_await_next_reply`` hands back our own prompt as if it were the user's
    # answer and the collector never actually waits for a reply. Irrelevant
    # (and skipped) in push mode, where replies never transit ``read_thread``.
    seen = 0 if await_reply is not None else len(await adapter.read_thread(conversation_ref))

    async def _next_reply_text() -> str | None:
        nonlocal seen
        if await_reply is not None:
            return await await_reply(request.timeout_seconds)
        reply = await _await_next_reply(
            adapter,
            conversation_ref,
            seen,
            timeout_seconds=request.timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        if reply is None:
            return None
        seen += 1
        return reply.text

    if request.title:
        await adapter.send_message(OutboundMessage(text=request.title, conversation_ref=conversation_ref))
        seen += 1

    for input_field in request.fields:
        error: str | None = None
        collected = False

        for _attempt in range(max_attempts):
            await adapter.send_message(
                OutboundMessage(text=_render_prompt(input_field, error), conversation_ref=conversation_ref)
            )
            seen += 1

            reply_text = await _next_reply_text()
            if reply_text is None:
                return InputResponse(values=values, status="timed_out")

            raw = reply_text.strip()
            if raw.lower() == _CANCEL_KEYWORD:
                return InputResponse(values=values, status="cancelled")

            try:
                values[input_field.key] = _coerce_and_validate(input_field, raw)
            except ValueError as exc:
                error = str(exc)
                continue

            collected = True
            break

        if not collected:
            return InputResponse(values=values, status="cancelled")

    return InputResponse(values=values, status="completed")


async def _await_next_reply(
    adapter: "ChatAdapter",
    conversation_ref: ConversationRef,
    seen_count: int,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> InboundMessage | None:
    """Poll ``read_thread`` until a message beyond ``seen_count`` appears, or time out."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while True:
        thread = await adapter.read_thread(conversation_ref)
        if len(thread) > seen_count:
            return thread[seen_count]
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(min(poll_interval_seconds, max(deadline - loop.time(), 0)))


def _render_prompt(input_field: InputField, error: str | None) -> str:
    lines = []
    if error:
        lines.append(f":x: {error}")

    if input_field.type is InputFieldType.CHOICE:
        options = input_field.options or []
        numbered = "\n".join(f"  {i}. {option}" for i, option in enumerate(options, start=1))
        lines.append(f"{input_field.label}\n{numbered}")
    else:
        lines.append(input_field.label)

    if input_field.type is InputFieldType.SECRET:
        lines.append(_SECRET_VISIBILITY_WARNING)

    return "\n".join(lines)


def _coerce_and_validate(input_field: InputField, raw: str) -> str:
    """Turn a raw reply into the field's stored value, or raise ``ValueError``."""
    if not raw:
        if input_field.required:
            raise ValueError(f"{input_field.label} is required.")
        return raw

    if input_field.type is InputFieldType.CHOICE:
        value = _resolve_choice(input_field, raw)
    elif input_field.type is InputFieldType.INT:
        if not re.fullmatch(r"-?\d+", raw):
            raise ValueError(f"{input_field.label} must be a whole number.")
        value = raw
    else:
        value = raw

    run_validator(input_field, value)
    return value


def _resolve_choice(input_field: InputField, raw: str) -> str:
    options = input_field.options or []
    try:
        index = int(raw) - 1
    except ValueError:
        raise ValueError(f"Enter a number between 1 and {len(options)}.") from None
    if not 0 <= index < len(options):
        raise ValueError(f"Enter a number between 1 and {len(options)}.")
    return options[index]


def run_validator(input_field: InputField, value: str) -> None:
    """Apply ``input_field.validator`` to ``value``; raise ``ValueError`` on failure.

    Shared by the scripted collector here and form-mode fulfilments (the Slack
    modal submission handler) so both modes enforce identical field rules.
    """
    validator = input_field.validator
    if validator is None:
        return

    if isinstance(validator, re.Pattern):
        ok = validator.fullmatch(value) is not None
    else:
        try:
            ok = bool(validator(value))
        except Exception as exc:
            raise ValueError(f"{input_field.label} is invalid: {exc}") from exc

    if not ok:
        raise ValueError(f"{input_field.label} is invalid.")
