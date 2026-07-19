"""Slack-native modal fulfilment for :class:`~router.chat.types.InputRequest`.

The generic bridge between the transport-neutral structured-input primitive
(#746) and Slack's ``views.open`` / ``view_submission`` machinery:

* :func:`build_input_request_view` renders any ``InputRequest`` as a Block
  Kit modal (one input block per field, ``block_id`` = field key).
* :func:`open_input_request` opens the modal and awaits the submission via a
  module-level pending-form registry keyed by a per-request ID carried in the
  view's ``private_metadata``.
* :func:`register_input_request_handlers` registers the ``view_submission`` /
  ``view_closed`` listeners on a Bolt app. The submission handler validates
  every field with the same rules the scripted fallback applies
  (:func:`router.chat.input_collect.run_validator` et al.) and re-prompts
  in-modal via ``response_action="errors"`` — Slack keeps the modal open with
  per-field error text, which is the modal equivalent of the scripted
  reprompt loop.

No caller builds Slack views directly anymore — collectors describe fields
as an ``InputRequest`` and this module owns every Slack-ism (issue #747).
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from router.chat.input_collect import run_validator
from router.chat.types import InputField, InputFieldType, InputRequest, InputResponse

if TYPE_CHECKING:
    from slack_bolt.async_app import AsyncApp

logger = logging.getLogger(__name__)

# Every InputRequest modal shares one callback so a single pair of Bolt
# listeners serves all collectors; the per-request identity travels in
# ``private_metadata``.
INPUT_REQUEST_CALLBACK_ID = "chat_input_request"

# All input elements use the same action_id — block_id (the field key) is the
# discriminator, mirroring how InputResponse.values is keyed.
_ACTION_ID = "value"

# Slack hard limit for modal title text.
_TITLE_MAX_CHARS = 24

_INT_RE = re.compile(r"-?\d+")


@dataclass
class _PendingForm:
    """One open modal awaiting submission."""

    request: InputRequest
    future: asyncio.Future[dict[str, str] | None]  # values on submit, None on close


# request_id → pending form. Module-level (like the approvals/grants
# registries) so every per-agent Bolt app's listeners resolve into the same
# table regardless of which Socket Mode connection Slack delivers to.
_pending_forms: dict[str, _PendingForm] = {}


def _plain_text(text: str, *, max_chars: int | None = None) -> dict[str, Any]:
    if max_chars is not None and len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return {"type": "plain_text", "text": text}


def _element_for_field(input_field: InputField) -> dict[str, Any]:
    """Render one field's input element. Slack-isms live here and nowhere else."""
    if input_field.type is InputFieldType.CHOICE:
        return {
            "type": "static_select",
            "action_id": _ACTION_ID,
            "options": [
                {"text": _plain_text(option, max_chars=75), "value": option} for option in input_field.options or []
            ],
        }
    if input_field.type is InputFieldType.CONVERSATION:
        return {
            "type": "conversations_select",
            "action_id": _ACTION_ID,
            "default_to_current_conversation": True,
            "filter": {"include": ["public", "private", "im"]},
        }
    element: dict[str, Any] = {"type": "plain_text_input", "action_id": _ACTION_ID}
    if input_field.multiline:
        element["multiline"] = True
    return element


def build_input_request_view(request: InputRequest, request_id: str) -> dict[str, Any]:
    """Render ``request`` as a Slack modal view payload."""
    blocks = []
    for input_field in request.fields:
        block: dict[str, Any] = {
            "type": "input",
            "block_id": input_field.key,
            "label": _plain_text(input_field.label),
            "element": _element_for_field(input_field),
        }
        if not input_field.required:
            block["optional"] = True
        blocks.append(block)

    return {
        "type": "modal",
        "callback_id": INPUT_REQUEST_CALLBACK_ID,
        "private_metadata": request_id,
        "notify_on_close": True,
        "title": _plain_text(request.title or "Input", max_chars=_TITLE_MAX_CHARS),
        "submit": _plain_text("Submit"),
        "close": _plain_text("Cancel"),
        "blocks": blocks,
    }


def _extract_raw_value(state_values: dict[str, Any], input_field: InputField) -> str:
    """Pull one field's raw value out of the ``view_submission`` state payload."""
    entry = (state_values.get(input_field.key, {}) or {}).get(_ACTION_ID, {}) or {}
    if input_field.type is InputFieldType.CHOICE:
        return ((entry.get("selected_option") or {}).get("value") or "").strip()
    if input_field.type is InputFieldType.CONVERSATION:
        return (entry.get("selected_conversation") or "").strip()
    return (entry.get("value") or "").strip()


def _validate_field(input_field: InputField, raw: str) -> str:
    """Modal-side twin of the scripted collector's field validation.

    Raises ``ValueError`` with user-facing text; the submission handler maps
    it onto the field's block via ``response_action="errors"``. CHOICE values
    arrive as the selected option itself (not a digit), so only membership is
    checked before the shared validator runs.
    """
    if not raw:
        if input_field.required:
            raise ValueError(f"{input_field.label} is required.")
        return raw

    if input_field.type is InputFieldType.INT and not _INT_RE.fullmatch(raw):
        raise ValueError(f"{input_field.label} must be a whole number.")
    if input_field.type is InputFieldType.CHOICE and raw not in (input_field.options or []):
        raise ValueError(f"{input_field.label} must be one of the listed options.")

    run_validator(input_field, raw)
    return raw


def validate_submission(request: InputRequest, view: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Validate a ``view_submission`` payload against ``request``.

    Returns ``(values, errors)`` — ``errors`` maps field key (= block_id) to
    user-facing message, empty when the submission is fully valid.
    """
    state_values = (view.get("state") or {}).get("values") or {}
    values: dict[str, str] = {}
    errors: dict[str, str] = {}
    for input_field in request.fields:
        try:
            values[input_field.key] = _validate_field(input_field, _extract_raw_value(state_values, input_field))
        except ValueError as exc:
            errors[input_field.key] = str(exc)
    return values, errors


async def open_input_request(client: Any, trigger_id: str, request: InputRequest) -> InputResponse:
    """Open ``request`` as a modal and await the user's submission.

    Resolution paths: a fully-valid submission → ``completed``; the user
    closing/cancelling the modal → ``cancelled``; no terminal interaction
    within ``request.timeout_seconds`` → ``timed_out`` (a submission arriving
    after that finds no pending entry and is acked-and-closed silently).

    Raises whatever ``views.open`` raises (invalid/expired trigger_id, API
    errors) — the caller owns user-facing failure messaging.
    """
    request_id = uuid.uuid4().hex
    future: asyncio.Future[dict[str, str] | None] = asyncio.get_running_loop().create_future()
    _pending_forms[request_id] = _PendingForm(request=request, future=future)
    try:
        await client.views_open(trigger_id=trigger_id, view=build_input_request_view(request, request_id))
        values = await asyncio.wait_for(future, timeout=request.timeout_seconds)
    except asyncio.TimeoutError:
        return InputResponse(values={}, status="timed_out")
    finally:
        _pending_forms.pop(request_id, None)

    if values is None:
        return InputResponse(values={}, status="cancelled")
    return InputResponse(values=values, status="completed")


def register_input_request_handlers(bolt_app: "AsyncApp") -> None:
    """Register the shared ``view_submission`` / ``view_closed`` listeners.

    Call once per Bolt app (alongside the app's other handler registrations).
    The listeners consult the module-level registry, so registering on many
    per-agent apps is safe — whichever app receives the interaction resolves
    the same pending form.
    """

    @bolt_app.view(INPUT_REQUEST_CALLBACK_ID)
    async def _on_input_request_submission(ack: Any, body: dict[str, Any]) -> None:
        view = body.get("view") or {}
        request_id = view.get("private_metadata") or ""
        pending = _pending_forms.get(request_id)
        if pending is None or pending.future.done():
            # Collector timed out or is gone — close the stale modal quietly.
            logger.info("InputRequest submission for unknown/expired request_id=%s", request_id)
            await ack()
            return

        values, errors = validate_submission(pending.request, view)
        if errors:
            # The modal-mode reprompt: Slack keeps the view open and renders
            # each message under its field.
            await ack(response_action="errors", errors=errors)
            return

        await ack()
        pending.future.set_result(values)

    @bolt_app.view_closed(INPUT_REQUEST_CALLBACK_ID)
    async def _on_input_request_closed(ack: Any, body: dict[str, Any]) -> None:
        await ack()
        request_id = (body.get("view") or {}).get("private_metadata") or ""
        pending = _pending_forms.get(request_id)
        if pending is not None and not pending.future.done():
            pending.future.set_result(None)
