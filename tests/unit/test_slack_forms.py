"""Tests for the generic Slack-modal InputRequest fulfilment (#747).

Covers ``router.chat.adapters.slack_forms``: the InputRequest → Block Kit
view builder, the shared view_submission/view_closed listeners (including the
``response_action="errors"`` in-modal reprompt), and the
``open_input_request`` pending-form lifecycle. No real Slack API is touched —
the Bolt app and WebClient are stubs.
"""

from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from router.chat.adapters import slack_forms
from router.chat.adapters.slack_forms import (
    INPUT_REQUEST_CALLBACK_ID,
    build_input_request_view,
    open_input_request,
    register_input_request_handlers,
    validate_submission,
)
from router.chat.types import InputField, InputFieldType, InputRequest

pytestmark = pytest.mark.unit


def _request(**overrides) -> InputRequest:
    defaults = dict(
        title="New task for Lisa",
        fields=[
            InputField(key="name", label="Name"),
            InputField(key="prompt", label="Prompt", multiline=True),
            InputField(key="destination", label="Destination", type=InputFieldType.CONVERSATION),
            InputField(key="timeout", label="Timeout", type=InputFieldType.INT, required=False),
        ],
        timeout_seconds=5,
    )
    defaults.update(overrides)
    return InputRequest(**defaults)


def _capture_handlers():
    """Register the listeners on a stub Bolt app; return them by name."""
    captured: dict[str, object] = {}
    bolt = MagicMock()

    def _view(callback_id):
        assert callback_id == INPUT_REQUEST_CALLBACK_ID

        def _decorator(fn):
            captured["submission"] = fn
            return fn

        return _decorator

    def _view_closed(callback_id):
        assert callback_id == INPUT_REQUEST_CALLBACK_ID

        def _decorator(fn):
            captured["closed"] = fn
            return fn

        return _decorator

    bolt.view = _view
    bolt.view_closed = _view_closed
    register_input_request_handlers(bolt)
    return captured


def _submission_view(request_id: str, values: dict[str, dict]) -> dict:
    return {"private_metadata": request_id, "state": {"values": values}}


def _state(name="Review", prompt_text="Do it", destination="C_DEST", timeout=""):
    return {
        "name": {"value": {"value": name}},
        "prompt": {"value": {"value": prompt_text}},
        "destination": {"value": {"selected_conversation": destination} if destination else {}},
        "timeout": {"value": {"value": timeout}},
    }


# ---------------------------------------------------------------------------
# View builder
# ---------------------------------------------------------------------------


class TestBuildView:
    def test_modal_shape(self):
        view = build_input_request_view(_request(), "req-1")
        assert view["type"] == "modal"
        assert view["callback_id"] == INPUT_REQUEST_CALLBACK_ID
        assert view["private_metadata"] == "req-1"
        assert view["notify_on_close"] is True
        assert view["title"]["text"] == "New task for Lisa"

    def test_title_truncated_to_slack_limit(self):
        view = build_input_request_view(_request(title="X" * 60), "req-1")
        assert len(view["title"]["text"]) <= 24

    def test_one_input_block_per_field_keyed_by_field_key(self):
        view = build_input_request_view(_request(), "req-1")
        assert [b["block_id"] for b in view["blocks"]] == ["name", "prompt", "destination", "timeout"]
        assert all(b["type"] == "input" for b in view["blocks"])

    def test_text_field_renders_plain_text_input(self):
        view = build_input_request_view(_request(), "req-1")
        name_block = view["blocks"][0]
        assert name_block["element"]["type"] == "plain_text_input"
        assert "multiline" not in name_block["element"]

    def test_multiline_hint_respected(self):
        view = build_input_request_view(_request(), "req-1")
        prompt_block = view["blocks"][1]
        assert prompt_block["element"]["multiline"] is True

    def test_conversation_field_renders_conversations_select(self):
        view = build_input_request_view(_request(), "req-1")
        dest_block = view["blocks"][2]
        assert dest_block["element"]["type"] == "conversations_select"
        assert dest_block["element"]["default_to_current_conversation"] is True
        assert "im" in dest_block["element"]["filter"]["include"]
        assert "public" in dest_block["element"]["filter"]["include"]
        # Required — no `optional: true`, so Slack enforces it client-side.
        assert dest_block.get("optional") is not True

    def test_optional_field_marked_optional(self):
        view = build_input_request_view(_request(), "req-1")
        timeout_block = view["blocks"][3]
        assert timeout_block["optional"] is True

    def test_choice_field_renders_static_select(self):
        request = InputRequest(
            title="Pick",
            fields=[InputField(key="color", label="Color", type=InputFieldType.CHOICE, options=["red", "green"])],
        )
        view = build_input_request_view(request, "req-1")
        element = view["blocks"][0]["element"]
        assert element["type"] == "static_select"
        assert [o["value"] for o in element["options"]] == ["red", "green"]


# ---------------------------------------------------------------------------
# Submission validation
# ---------------------------------------------------------------------------


class TestValidateSubmission:
    def test_valid_submission_returns_values_no_errors(self):
        values, errors = validate_submission(_request(), _submission_view("r", _state(timeout="120")))
        assert errors == {}
        assert values == {"name": "Review", "prompt": "Do it", "destination": "C_DEST", "timeout": "120"}

    def test_missing_required_field_errors(self):
        values, errors = validate_submission(_request(), _submission_view("r", _state(name="")))
        assert errors == {"name": "Name is required."}

    def test_missing_destination_errors(self):
        _, errors = validate_submission(_request(), _submission_view("r", _state(destination="")))
        assert "destination" in errors

    def test_optional_empty_field_ok(self):
        values, errors = validate_submission(_request(), _submission_view("r", _state(timeout="")))
        assert errors == {}
        assert values["timeout"] == ""

    def test_int_field_rejects_non_numeric(self):
        _, errors = validate_submission(_request(), _submission_view("r", _state(timeout="abc")))
        assert errors == {"timeout": "Timeout must be a whole number."}

    def test_validator_failure_reported_per_field(self):
        request = _request()
        request.fields[0] = InputField(key="name", label="Name", validator=re.compile(r"[a-z-]+"))
        _, errors = validate_submission(request, _submission_view("r", _state(name="NOPE!!")))
        assert "name" in errors

    def test_raising_validator_message_included(self):
        def _cron_like(value: str) -> bool:
            raise ValueError("bad schedule shape")

        request = _request()
        request.fields[0] = InputField(key="name", label="Cron", validator=_cron_like)
        _, errors = validate_submission(request, _submission_view("r", _state(name="whatever")))
        assert "bad schedule shape" in errors["name"]

    def test_choice_membership_enforced(self):
        request = InputRequest(
            title="Pick",
            fields=[InputField(key="color", label="Color", type=InputFieldType.CHOICE, options=["red", "green"])],
        )
        view = _submission_view("r", {"color": {"value": {"selected_option": {"value": "blue"}}}})
        _, errors = validate_submission(request, view)
        assert "color" in errors

    def test_choice_selected_option_extracted(self):
        request = InputRequest(
            title="Pick",
            fields=[InputField(key="color", label="Color", type=InputFieldType.CHOICE, options=["red", "green"])],
        )
        view = _submission_view("r", {"color": {"value": {"selected_option": {"value": "green"}}}})
        values, errors = validate_submission(request, view)
        assert errors == {}
        assert values == {"color": "green"}


# ---------------------------------------------------------------------------
# open_input_request + listeners — full lifecycle
# ---------------------------------------------------------------------------


class TestModalLifecycle:
    def _client(self):
        client = MagicMock()
        client.views_open = AsyncMock(return_value={"ok": True})
        return client

    async def _open(self, client, request):
        collect = asyncio.create_task(open_input_request(client, "T123", request))
        for _ in range(100):
            if client.views_open.await_count:
                break
            await asyncio.sleep(0.01)
        view = client.views_open.call_args.kwargs["view"]
        return collect, view

    @pytest.mark.asyncio
    async def test_valid_submission_completes(self):
        handlers = _capture_handlers()
        client = self._client()
        collect, view = await self._open(client, _request())
        assert client.views_open.call_args.kwargs["trigger_id"] == "T123"

        ack = AsyncMock()
        await handlers["submission"](ack, {"view": _submission_view(view["private_metadata"], _state(timeout="90"))})

        response = await asyncio.wait_for(collect, timeout=5)
        assert response.status == "completed"
        assert response.values == {"name": "Review", "prompt": "Do it", "destination": "C_DEST", "timeout": "90"}
        # Plain ack — Slack closes the modal.
        ack.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_invalid_submission_repromts_in_modal_then_completes(self):
        handlers = _capture_handlers()
        client = self._client()
        collect, view = await self._open(client, _request())
        request_id = view["private_metadata"]

        bad_ack = AsyncMock()
        await handlers["submission"](bad_ack, {"view": _submission_view(request_id, _state(name=""))})
        bad_ack.assert_awaited_once_with(response_action="errors", errors={"name": "Name is required."})
        assert not collect.done()

        good_ack = AsyncMock()
        await handlers["submission"](good_ack, {"view": _submission_view(request_id, _state())})
        response = await asyncio.wait_for(collect, timeout=5)
        assert response.status == "completed"

    @pytest.mark.asyncio
    async def test_close_cancels(self):
        handlers = _capture_handlers()
        client = self._client()
        collect, view = await self._open(client, _request())

        ack = AsyncMock()
        await handlers["closed"](ack, {"view": {"private_metadata": view["private_metadata"]}})

        response = await asyncio.wait_for(collect, timeout=5)
        assert response.status == "cancelled"
        assert response.values == {}

    @pytest.mark.asyncio
    async def test_no_interaction_times_out(self):
        client = self._client()
        response = await open_input_request(client, "T123", _request(timeout_seconds=0.05))
        assert response.status == "timed_out"

    @pytest.mark.asyncio
    async def test_submission_for_expired_form_acks_quietly(self):
        handlers = _capture_handlers()
        ack = AsyncMock()
        await handlers["submission"](ack, {"view": _submission_view("gone-request", _state())})
        ack.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_close_for_expired_form_is_noop(self):
        handlers = _capture_handlers()
        ack = AsyncMock()
        await handlers["closed"](ack, {"view": {"private_metadata": "gone-request"}})
        ack.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_views_open_failure_propagates_and_cleans_up(self):
        client = MagicMock()
        client.views_open = AsyncMock(side_effect=RuntimeError("expired trigger"))
        with pytest.raises(RuntimeError, match="expired trigger"):
            await open_input_request(client, "T123", _request())
        assert slack_forms._pending_forms == {}
