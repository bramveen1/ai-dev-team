"""Transport-neutral command dispatcher with human-only authz gate.

All verb handlers invoked here return a :class:`~router.commands.types.CommandResult`
DTO — they never reference a transport's ``respond()`` or ``ack()`` primitives.
The per-transport entry shims (Slack, Discord) call :func:`dispatch_command` and
render the result in whatever way the transport supports.

Human-only gate
---------------
Every mutating verb (``kill``, ``killall``, ``tasks``, ``grant``, ``revoke``,
``list packs``, ``who has``, ``approve``, ``reject``) is restricted to human
principals.  Only ``help`` is allowed for any principal kind.  This gate lives
here in *core* rather than in per-transport shims so the invariant cannot be
bypassed by adding a new transport shim that forgets the check.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from router.commands.grammar import help_text
from router.commands.types import Command, CommandResult, Principal

if TYPE_CHECKING:
    from router.approvals.store import DraftStore
    from router.stuck_guard import StuckGuard

logger = logging.getLogger(__name__)

# Verbs that only humans may invoke.  ``help`` is explicitly excluded so
# agents can always ask for help text (read-only, non-destructive).
HUMAN_ONLY_VERBS: frozenset[str] = frozenset(
    {
        "kill",
        "killall",
        "tasks",
        "grant",
        "revoke",
        "list packs",
        "who has",
        "approve",
        "reject",
    }
)

ActiveAgentResolver = Any  # Callable[[str, str], str | None]


async def dispatch_command(
    cmd: Command,
    principal: Principal,
    *,
    subject_agent: str | None = None,
    guard: "StuckGuard | None" = None,
    active_agent_resolver: ActiveAgentResolver = None,
    client: Any = None,
    tasks_store: Any = None,
    draft_store: "DraftStore | None" = None,
) -> CommandResult:
    """Gate + route a parsed :class:`~router.commands.types.Command`.

    Parameters
    ----------
    cmd:
        Parsed command from any transport's entry shim.
    principal:
        The entity that issued the command.  ``kind="bot"`` rejects all
        mutating verbs with a clear error.
    subject_agent:
        The agent this command is addressed to (e.g. the Discord bot's own
        ``agent_name``).  Used as the target for ``kill`` and ``tasks``.
    guard:
        The :class:`~router.stuck_guard.StuckGuard` instance.  When ``None``
        the default guard singleton is used.
    active_agent_resolver:
        ``Callable[[channel, thread_ts], agent_name | None]`` — resolves the
        active agent for the thread.  Optional; falls back to ``subject_agent``
        when not provided.
    client:
        Transport client forwarded to kill notifications (Slack-only path).
    tasks_store:
        :class:`~router.scheduled_tasks.store.ScheduledTaskStore` to use for
        task listing.  When ``None`` tasks subcommands return an error.
    draft_store:
        :class:`~router.approvals.store.DraftStore` to use for approve/reject
        commands.  When ``None`` approval subcommands return an error.
    """
    if principal.kind == "bot" and cmd.verb in HUMAN_ONLY_VERBS:
        return CommandResult(
            text=(f"agents cannot run commands — {cmd.verb!r} is restricted to human principals"),
            ok=False,
        )

    if cmd.verb == "help":
        arg = cmd.args[0] if cmd.args else None
        return CommandResult(text=help_text(arg))

    if cmd.verb in ("kill", "killall"):
        from router.kill_command import execute_kill_command

        return await execute_kill_command(
            cmd,
            guard=guard,
            active_agent_resolver=active_agent_resolver
            or ((lambda _ch, _th: subject_agent) if subject_agent else None),
            client=client,
        )

    if cmd.verb == "tasks":
        from router.scheduled_tasks.handlers import execute_tasks_command

        return await execute_tasks_command(
            cmd,
            subject_agent=subject_agent,
            store=tasks_store,
        )

    if cmd.verb in ("approve", "reject"):
        from router.commands.approve import execute_approval_command

        return await execute_approval_command(cmd, draft_store)

    return CommandResult(
        text=f"error: {cmd.verb!r} not yet implemented for {cmd.transport!r} transport",
        ok=False,
    )
