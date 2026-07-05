"""Terminal console — talk to an agent from the command line.

Invoke inside the router container:

    python -m router.chat.terminal_console [--agent <name>]

Reads plain text from stdin, routes each line through ``run_agent_turn``,
and prints the agent's reply to stdout. Router diagnostic logs are
redirected to stderr so they don't interleave with the conversation.

This module is a thin operator console, not the E2E primitive driver
(``terminal_driver``). The driver exercises all four adapter primitives
in isolation; this module provides the user-facing conversation loop
backed by real agent output.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid

from router.chat.adapters.terminal import TerminalAdapter, make_inbound_ref
from router.chat.core import run_agent_turn
from router.chat.types import InboundMessage

logger = logging.getLogger(__name__)


def _redirect_router_logs_to_stderr() -> None:
    """Route router.* logs to stderr so they don't pollute the conversation output."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root = logging.getLogger("router")
    root.handlers = [handler]
    root.propagate = False


async def _console_loop(agent_name: str) -> None:
    adapter = TerminalAdapter()
    session_id = str(uuid.uuid4())[:8]
    conversation_ref = make_inbound_ref(session_id)
    principal_ref = adapter.resolve_principal(adapter._username)

    print(f"Talking to {agent_name}. Ctrl-C or Ctrl-D to exit.\n", flush=True)

    loop = asyncio.get_running_loop()
    while True:
        print("> ", end="", flush=True)
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
        except KeyboardInterrupt:
            break
        if not line:  # EOF
            break
        text = line.rstrip("\n")
        if not text:
            continue

        inbound = InboundMessage(
            conversation_ref=conversation_ref,
            principal_ref=principal_ref,
            text=text,
        )

        try:
            await run_agent_turn(adapter, inbound, agent_name=agent_name)
        except Exception as exc:
            print(f"[error] {exc}", flush=True)

        # Record after the turn so read_thread() during the turn does not
        # include the current message — preventing it from being double-counted
        # (once in thread history, once as new_message in build_full_context).
        adapter.record_inbound(inbound)

        print(flush=True)


def main() -> None:
    """Entry point for ``python -m router.chat.terminal_console``."""
    parser = argparse.ArgumentParser(description="Terminal console — talk to an AI dev team agent.")
    parser.add_argument(
        "--agent",
        default=None,
        help="Agent to talk to (default: DEFAULT_AGENT setting, else first discovered agent).",
    )
    args = parser.parse_args()

    _redirect_router_logs_to_stderr()

    from router.config import resolve_default_agent  # noqa: PLC0415 — after log redirect

    asyncio.run(_console_loop(args.agent or resolve_default_agent()))


if __name__ == "__main__":
    main()
