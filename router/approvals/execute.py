"""Approved-draft execution — turns an Approve click into the real action.

Wired in as ``execute_callback`` on the approval handlers by ``router.app``.
The agent drafted the action originally; on approval we either docker-exec
the dispatch handler directly (``dispatch.dispatch_issue`` drafts, issue
#212) or re-enter the owning agent's container with a synthesized "execute
it now" prompt (all other drafts).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from router.approvals.store import Draft
from router.config import get_agent_map
from router.container_exec import run_in_container as _run_in_container
from router.dispatcher import dispatch
from router.packs.dispatch_hook import pack_cli_extras
from router.runtime import bot_user_map
from router.runtime import workers_client as _workers_client
from router.slack_format import md_to_slack
from router.slack_users import outbound_mention_ids

logger = logging.getLogger(__name__)

# Router config dict, injected by ``router.app`` at import (and again on
# reload) via :func:`configure` so both modules share one loaded config.
config: dict = {}


def configure(router_config: dict) -> None:
    """Inject the loaded router config (called by ``router.app``)."""
    global config
    config = router_config


async def _execute_approved_draft(draft: Draft, channel: str, thread_ts: str, client: Any) -> None:
    """Re-dispatch to the owning agent so it actually runs the approved action.

    The agent drafted the action originally — by re-entering its container
    with the approved draft's metadata as the message, it can execute via the
    same pack tools (``gh pr merge``, etc.) it had access to during drafting.

    The agent's response is parsed for further draft blocks (rare) and
    posted back into the same thread, mirroring the regular event path.

    Special case (issue #212): ``dispatch.dispatch_issue`` approvals skip
    the agent CLI re-entry path entirely. Instead, the router shells out
    via ``docker exec`` to ``packs.dispatch.handler.dispatch_issue`` in
    the owning agent's container with ``--approved`` (which sets
    ``_approved=True`` so the gate is bypassed) and
    ``--supervision-mode poll`` (so the handler returns ``launched`` in
    ~3 s instead of blocking until the spawned ``claude -p`` finishes).
    The router itself has no ``~/.claude/`` so calling the handler
    in-process raises ``auth_seed_failed`` (issue #219); running it
    inside the agent container gives the dispatch its normal
    credentials. The terminal ``:white_check_mark: / :x:`` envelope
    arrives later through the router-side discovery + supervision
    loops, not through the docker-exec stdout parse below.
    """
    if draft.capability_instance == "dispatch" and draft.action_verb == "dispatch_issue":
        # Issue #270: every ack in this branch reports *on a dispatch* —
        # launched / done / error envelopes — so it speaks as the workers bot,
        # not the agent persona whose bolt client handled the approval click.
        # Falls back to the agent ``client`` when ``WORKERS_BOT_TOKEN`` is unset.
        # Discord-origin drafts always use the passed-in client instead: it's
        # a chat_postMessage-shaped facade already bound to the originating
        # Discord conversation, and the Slack workers bot has no Discord
        # identity to post through (#682).
        is_discord_origin = (draft.payload or {}).get("transport") == "discord"
        lifecycle_client = client if is_discord_origin else (_workers_client() or client)

        # draft.payload mirrors the gate_preview dict produced by
        # packs.dispatch.handler._evaluate_approval_gate — its keys are
        # (issue_url, repo, branch_target, model, est_workspace_path,
        # gate_reason, budget_seconds, persona, …), NOT the dispatch_issue()
        # kwargs. budget_seconds/persona are optional (#798) — absent on
        # legacy/partial payloads, in which case the handler's own defaults
        # apply on re-execute.
        payload = draft.payload or {}
        issue_url = payload.get("issue_url") or ""
        pr_url_payload = payload.get("pr_url") or ""
        # In existing-PR mode pr_url is set without an issue_url; require at least one.
        if not issue_url and not pr_url_payload:
            logger.error(
                "Approved dispatch_issue draft %s has no issue_url or pr_url in payload — cannot execute",
                draft.draft_id,
            )
            await lifecycle_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":x: Approved draft `{draft.draft_id}` missing issue_url/pr_url; nothing executed.",
            )
            return

        agent_name = draft.agent_name
        agent_map = get_agent_map()
        if agent_name not in agent_map:
            logger.error(
                "Approved dispatch_issue draft %s names unknown agent %r — cannot execute",
                draft.draft_id,
                agent_name,
            )
            await lifecycle_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":x: Approved draft references unknown agent `{agent_name}`; nothing executed.",
            )
            return

        container = agent_map[agent_name]["container"]
        # Run the handler inside the originating agent's container so it can
        # read ~/.claude/ (the router container has no Claude credentials).
        #
        # ``--supervision-mode poll`` is mandatory here. Without it the
        # handler defaults to ``_supervision_mode()`` which reads
        # ``$DISPATCH_SUPERVISION`` from the agent container's env — that
        # var is unset on every agent in docker-compose, so the handler
        # would fall back to inline mode and block for the full ~30 min
        # budget waiting on the spawned ``claude -p``. The ``docker exec``
        # we're about to make is capped at 120 s, so inline mode would
        # always time out, post a false ``execution failed``, and orphan
        # the (already-spawned) ``claude -p`` grandchild as a PID-1 leak
        # in the agent container — see issue #212 (post-#221) write-up.
        # poll mode returns ``{status: launched, ...}`` in ~3 s; the
        # router-side discovery + supervision loops post the terminal
        # envelope from there.
        cmd = [
            "python",
            "/config/packs/dispatch/handler.py",
            "dispatch_issue",
        ]
        if issue_url:
            cmd += ["--issue-url", issue_url]
        if payload.get("pr_url"):
            cmd += ["--pr-url", str(payload["pr_url"])]
        cmd += [
            "--channel",
            channel,
            "--thread-ts",
            thread_ts,
            "--agent",
            agent_name,
            "--approved",
            "--supervision-mode",
            "poll",
        ]
        if "model" in payload:
            cmd += ["--model", payload["model"]]
        if "persona" in payload:
            cmd += ["--persona", str(payload["persona"])]
        if "budget_seconds" in payload:
            cmd += ["--budget-seconds", str(payload["budget_seconds"])]
        if payload.get("summary"):
            cmd += ["--summary", payload["summary"]]

        logger.info(
            "gate_bypass_via_approval: executing dispatch_issue via docker exec agent=%s container=%s draft=%s",
            agent_name,
            container,
            draft.draft_id,
        )

        # Inject pack-derived env (notably ``WORKERS_BOT_TOKEN``) via the
        # same hook the agent-initiated dispatch path uses
        # (``router/dispatcher.py``). Without this, the handler's #257
        # guard fires ``workers_token_missing`` on every approval-card
        # dispatch — see issue #268. ``pack_cli_extras`` also resolves
        # any pack-declared secret env for the agent, so future packs
        # (github, browser_use, …) pick up symmetric treatment on both
        # docker-exec paths for free.
        # #665: thread originating transport so Discord-origin approved drafts
        # execute on the Discord transport rather than defaulting to Slack.
        _transport = payload.get("transport") or ""
        _conv_id = payload.get("conversation_id") or ""
        conversation_ref = _conv_id if (_transport == "discord" and _conv_id) else None
        extras = pack_cli_extras(agent_name, channel=channel, thread_ts=thread_ts, conversation_ref=conversation_ref)

        try:
            stdout, stderr, _rc = await _run_in_container(
                container=container,
                command=cmd,
                timeout=120,
                env=extras.env or None,
            )
        except Exception:
            logger.exception("docker exec dispatch_issue failed for approved draft %s", draft.draft_id)
            await lifecycle_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":x: Approved, but execution failed for draft `{draft.draft_id}`. Check router logs.",
            )
            return

        try:
            result: dict[str, Any] = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            logger.error(
                "dispatch_issue stdout not valid JSON for draft %s; stderr=%r stdout=%r",
                draft.draft_id,
                stderr[:200],
                stdout[:200],
            )
            await lifecycle_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":x: Approved, but handler returned non-JSON for draft `{draft.draft_id}`. Check router logs.",
            )
            return

        status = result.get("status")
        dispatch_id = result.get("dispatch_id", draft.draft_id)
        if status == "launched":
            text = f":rocket: `{dispatch_id}` launched (approved)"
        elif status == "completed":
            text = f":white_check_mark: `{dispatch_id}` done (exit 0)"
        elif status == "failed":
            exitcode = result.get("exitcode", -1)
            if exitcode == -1:
                text = f":warning: `{dispatch_id}` terminated (exit -1)"
            else:
                text = f":x: `{dispatch_id}` failed (exit {exitcode})"
        elif status == "error":
            text = f":x: `{dispatch_id}` error: {result.get('reason', 'unknown')}"
        else:
            text = f":x: `{dispatch_id}` unexpected status: {status}"

        await lifecycle_client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
        return

    # All other drafts: agent CLI re-entry path.
    # Build a synthesized prompt the agent will recognize. Keep it tight
    # so the agent doesn't re-draft instead of executing.
    payload_summary = ", ".join(f"{k}={v}" for k, v in (draft.payload or {}).items() if v is not None)
    message = (
        f"The user just approved your earlier draft via the Slack approval card. "
        f"Execute it now. Do not draft again — just run the action and reply with a short confirmation.\n"
        f"\n"
        f"Action: {draft.action_verb}\n"
        f"Pack: {draft.capability_instance}\n"
        f"Payload: {payload_summary or '(none)'}"
    )

    agent_name = draft.agent_name
    agent_map = get_agent_map()
    if agent_name not in agent_map:
        logger.error("Approved draft %s names unknown agent %r — cannot execute", draft.draft_id, agent_name)
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f":x: Approved draft references unknown agent `{agent_name}`; nothing executed.",
        )
        return

    try:
        # A card approval is an explicit human action, so it resets the
        # stuck-guard turn cap and clears a guard-tripped halt for the thread
        # (#422) — otherwise an approved action could be silently refused on a
        # thread that had already tripped the cap.
        result = await dispatch(
            agent_name=agent_name,
            message=message,
            channel=channel,
            thread_ts=thread_ts,
            client=client,
            timeout=config.get("session_timeout"),
            bot_user_map=dict(bot_user_map),
            human_initiated=True,
        )
    except Exception:
        logger.exception("Failed to dispatch execution for approved draft %s", draft.draft_id)
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f":x: Approved, but execution failed for draft `{draft.draft_id}`. Check router logs.",
        )
        return

    response_text = result["response"]
    if response_text:
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=md_to_slack(response_text, await outbound_mention_ids(client)),
        )
