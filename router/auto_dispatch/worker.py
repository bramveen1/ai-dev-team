"""Worker dispatch helper — launches a real dev-worker for a candidate issue."""

from __future__ import annotations

import json
import logging
from typing import Any

from router import config, settings

logger = logging.getLogger(__name__)


async def _default_create_draft_fn(**kwargs: Any) -> None:
    """Default implementation: delegate to ``router.internal_api.create_dispatch_draft``."""
    from router.internal_api import create_dispatch_draft  # noqa: PLC0415

    await create_dispatch_draft(**kwargs)


async def _dispatch_worker(
    *,
    issue_url: str,
    issue_num: int,
    issue_title: str,
    slack_client: Any,
    destination: str | None,
    thread_ts: str = "",
    payload: dict,
    _create_draft_fn: Any = None,
    conversation_ref: str | None = None,
) -> str:
    """Launch a real dev-worker dispatch for *issue_url*.

    Calls the dispatch handler WITHOUT ``--approved`` so the approval gate
    (``approval.require_always`` or the cost/keyword smart gate) is evaluated
    normally.  Two outcomes are handled:

    * ``launched`` — worker spawned successfully; caller enrols in awaiting.
    * ``approval_required`` — the gate fired.  ``_create_draft_fn`` is called
      to persist a draft in the router's store and post the Block Kit approval
      card to Slack.  The human then clicks Approve; the approved-draft
      executor in ``router.approvals.execute`` re-invokes the handler with
      ``--approved``.  No worker is spawned here; the caller does NOT enrol
      in awaiting.

    Any other status raises ``RuntimeError`` so the caller can record the
    failure and avoid marking the issue as awaiting a PR that never started.

    The router process has no ``~/.claude/`` credentials, so calling the handler
    in-process raises ``auth_seed_failed`` (#219) — it MUST run in the agent
    container. This contract is the same one in
    ``router.approvals.execute._execute_approved_draft``; keep them in sync
    (#212/#219/#268).

    ``_create_draft_fn`` is injectable for unit tests; the default calls
    ``router.internal_api.create_dispatch_draft`` directly.
    """
    # Import lazily and from the primitive modules (NOT router.app) to avoid a
    # circular import and keep auto_dispatch removable as a unit.
    from router.dispatcher import _run_in_container  # noqa: PLC0415
    from router.packs.dispatch_hook import pack_cli_extras  # noqa: PLC0415

    agent_name = payload.get("worker_agent") or config.resolve_worker_agent()
    agent_map = config.get_agent_map()
    if agent_name not in agent_map:
        raise RuntimeError(f"auto_dispatch: unknown worker agent {agent_name!r}")
    container = agent_map[agent_name]["container"]

    channel = destination or settings.get("OPERATOR_DM_CHANNEL") or ""

    model = payload.get("worker_model", "sonnet") or "sonnet"
    cmd = [
        "python",
        "/config/packs/dispatch/handler.py",
        "dispatch_issue",
        "--issue-url",
        issue_url,
        "--channel",
        channel,
        "--thread-ts",
        thread_ts,
        "--agent",
        agent_name,
        # NOTE: --approved is intentionally omitted so the handler evaluates
        # the approval gate.  The auto path must NOT bypass it (#563).
        "--supervision-mode",
        "poll",
        "--persona",
        payload.get("worker_persona", "dev"),
        "--model",
        model,
    ]
    budget = payload.get("worker_budget_seconds")
    if budget:
        cmd += ["--budget-seconds", str(int(budget))]

    # Inject pack-derived env (notably WORKERS_BOT_TOKEN, #268) so the handler's
    # #257 guard doesn't fire workers_token_missing on the autonomous path.
    extras = pack_cli_extras(agent_name, channel=channel, thread_ts=thread_ts, conversation_ref=conversation_ref)

    logger.info(
        "auto_dispatch._dispatch_worker: docker-exec dispatch_issue for issue #%s in container=%s agent=%s",
        issue_num,
        container,
        agent_name,
    )
    # Keep the inner docker-exec hard-kill strictly *below* the caller's outer
    # ``wait_for`` budget (``payload["dispatch_timeout"]``, default 60s) so the
    # subprocess is reaped cleanly here rather than being abandoned when the
    # outer timeout cancels this coroutine. Poll mode returns ``launched`` in
    # ~3s, so this ceiling is only a safety backstop.
    exec_timeout = max(10, int(payload.get("dispatch_timeout", 60)) - 5)
    stdout, stderr, _rc = await _run_in_container(
        container=container,
        command=cmd,
        timeout=exec_timeout,
        env=extras.env or None,
    )
    try:
        result = json.loads(stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"auto_dispatch: handler returned non-JSON for issue #{issue_num}: "
            f"stdout={stdout[:200]!r} stderr={stderr[:200]!r}"
        ) from exc

    status = result.get("status")

    if status == "approval_required":
        # Gate fired — create a draft and post the approval card so a human can
        # approve.  Worker is NOT spawned here; the caller must not enrol in
        # awaiting (it checks the return value of _dispatch_worker indirectly
        # by catching exceptions — approval_required is NOT an error).
        gate_preview = result.get("preview") or {}
        budget_seconds = int(payload.get("worker_budget_seconds") or 1800)
        persona = payload.get("worker_persona", "dev") or "dev"
        create_fn = _create_draft_fn or _default_create_draft_fn
        await create_fn(
            agent_name=agent_name,
            channel=channel,
            thread_ts=thread_ts,
            issue_url=issue_url,
            issue_num=issue_num,
            issue_title=issue_title,
            model=model,
            persona=persona,
            budget_seconds=budget_seconds,
            gate_preview=gate_preview,
        )
        logger.info(
            "auto_dispatch: approval_required for issue #%s (gate_reason=%r); draft posted — awaiting human Approve",
            issue_num,
            gate_preview.get("gate_reason"),
        )
        return "approval_required"

    if status != "launched":
        raise RuntimeError(
            f"auto_dispatch: dispatch_issue for issue #{issue_num} returned status={status!r} "
            f"detail={result.get('reason') or result.get('detail')!r}"
        )
    logger.info(
        "auto_dispatch: launched worker dispatch %s for issue #%s",
        result.get("dispatch_id"),
        issue_num,
    )
    return "launched"
