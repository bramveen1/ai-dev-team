"""Minimal stdin/stdout E2E driver for the terminal ChatAdapter.

Drives the whole agent loop with no Slack creds, no network:
  read a line → build conversation_ref + principal_ref → dispatch
  into the agent → print outbound → render an interactive prompt as a
  numbered text reply.

This is not a user-facing operator console. It is the thinnest possible
skin over the terminal adapter to make the adapter loop runnable at a
prompt — a local dev/E2E harness that proves all four primitives work.
"""

from __future__ import annotations

import asyncio
import sys
import uuid

from router.chat.adapters.terminal import TerminalAdapter, make_inbound_ref
from router.chat.types import (
    AdapterStatus,
    InboundMessage,
    OutboundMessage,
    PromptChoice,
)


async def run_once(
    adapter: TerminalAdapter,
    text: str,
    *,
    session_id: str | None = None,
) -> None:
    """Drive one inbound message through all four adapter primitives.

    1. **Inbound** — build a non-Slack ``conversation_ref`` + ``principal_ref``.
    2. **Status** — signal THINKING via :meth:`~TerminalAdapter.set_status`.
    3. **Outbound** — echo reply via :meth:`~TerminalAdapter.send_message` with ref.
    4. **Proactive** — emit with ``conversation_ref=None`` (cron / dispatch-result shape).
    5. **Interactive** — :meth:`~TerminalAdapter.prompt_for_choice` as numbered list.
    """
    if session_id is None:
        session_id = str(uuid.uuid4())[:8]

    # 1. Inbound — adapter-private ref construction (only site for terminal refs)
    conversation_ref = make_inbound_ref(session_id)
    principal_ref = adapter.resolve_principal(adapter._username)

    inbound = InboundMessage(
        conversation_ref=conversation_ref,
        principal_ref=principal_ref,
        text=text,
    )
    adapter.record_inbound(inbound)
    mentions = adapter.parse_mentions(text, conversation_ref)

    # 2. Status
    await adapter.set_status(conversation_ref, AdapterStatus.THINKING)

    # 3. Outbound — reply using the stored ref
    reply_text = f"Echo: {text}"
    if mentions:
        reply_text += f" (mentions: {', '.join(mentions)})"
    await adapter.send_message(OutboundMessage(text=reply_text, conversation_ref=conversation_ref))

    # 4. Proactive — no inbound ref (cron / background-task shape)
    await adapter.send_message(OutboundMessage(text="[proactive] background task complete"))

    # 5. Interactive — driver calls prompt_for_choice directly because
    #    supports_interactive=False keeps core from doing it; numbered text
    #    exercises the path Slack buttons would never hit.
    choices_prompt = PromptChoice(
        prompt="What should I do next?",
        choices=["Continue", "Stop", "Restart"],
    )
    response = await adapter.prompt_for_choice(conversation_ref, choices_prompt)
    await adapter.send_message(OutboundMessage(text=f"You chose: {response.choice}", conversation_ref=conversation_ref))

    await adapter.set_status(conversation_ref, AdapterStatus.DONE)


async def _main() -> None:
    adapter = TerminalAdapter()
    print("Terminal E2E driver — type a message and press Enter.", file=sys.stdout, flush=True)
    print("Ctrl-C or Ctrl-D to exit.\n", file=sys.stdout, flush=True)

    loop = asyncio.get_running_loop()
    while True:
        print("> ", end="", file=sys.stdout, flush=True)
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
        except KeyboardInterrupt:
            break
        if not line:  # EOF
            break
        text = line.rstrip("\n")
        if not text:
            continue
        await run_once(adapter, text)
        print(file=sys.stdout, flush=True)


def main() -> None:
    """Entry point for the terminal E2E driver."""
    asyncio.run(_main())


if __name__ == "__main__":
    main()
