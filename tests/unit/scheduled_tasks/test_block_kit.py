"""Tests for scheduled task Block Kit builders."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from router.scheduled_tasks.block_kit import (
    ACTION_ID_CRON,
    ACTION_ID_NAME,
    BLOCK_ID_CRON,
    BLOCK_ID_DESTINATION,
    BLOCK_ID_NAME,
    BLOCK_ID_TIMEOUT,
    MODAL_CALLBACK_CREATE_TASK,
    build_create_task_modal,
    build_task_list_message,
    parse_create_modal_submission,
)
from router.scheduled_tasks.store import ScheduledTask


def _make_task(**overrides):
    defaults = {
        "task_id": str(uuid.uuid4()),
        "agent_name": "lisa",
        "name": "Inbox review",
        "prompt": "Do the thing",
        "schedule_cron": "0 9 * * 1-5",
        "next_run_at": datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc),
        "destination": None,
        "enabled": True,
        "created_at": datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return ScheduledTask(**defaults)


@pytest.mark.unit
class TestListMessage:
    def test_empty_state(self):
        msg = build_task_list_message("lisa", [])
        assert "no scheduled tasks" in str(msg["blocks"])

    def test_rendered_task_includes_key_fields(self):
        task = _make_task(name="Inbox review", destination="C_DEST")
        msg = build_task_list_message("lisa", [task])
        flat = str(msg["blocks"])
        assert "Inbox review" in flat
        assert task.task_id in flat
        assert "0 9 * * 1-5" in flat
        # Destination is rendered as a Slack channel mention so the user
        # sees the channel name, not the raw ID.
        assert "<#C_DEST>" in flat

    def test_rendered_task_marks_unset_destination(self):
        # Old rows from before the required-destination change may still be
        # null — the list view should make that explicit, not pretend it's
        # going to "agent DM" anywhere.
        task = _make_task(destination=None)
        flat = str(build_task_list_message("lisa", [task])["blocks"])
        assert "_unset_" in flat

    def test_paused_task_labeled(self):
        task = _make_task(enabled=False)
        flat = str(build_task_list_message("lisa", [task])["blocks"])
        assert "paused" in flat


@pytest.mark.unit
class TestCreateModal:
    def test_modal_has_required_blocks(self):
        modal = build_create_task_modal("lisa")
        assert modal["callback_id"] == MODAL_CALLBACK_CREATE_TASK
        assert modal["private_metadata"] == "lisa"
        block_ids = {b.get("block_id") for b in modal["blocks"] if "block_id" in b}
        assert BLOCK_ID_NAME in block_ids
        assert BLOCK_ID_CRON in block_ids
        assert BLOCK_ID_DESTINATION in block_ids
        assert BLOCK_ID_TIMEOUT in block_ids

    def test_destination_is_a_required_conversations_select(self):
        modal = build_create_task_modal("lisa")
        dest_block = next(b for b in modal["blocks"] if b.get("block_id") == BLOCK_ID_DESTINATION)
        # Required = no `optional: true`, so Slack enforces it client-side.
        assert dest_block.get("optional") is not True
        assert dest_block["element"]["type"] == "conversations_select"
        # Channel + DM coverage; no MPIM in the filter on purpose.
        assert "im" in dest_block["element"]["filter"]["include"]
        assert "public" in dest_block["element"]["filter"]["include"]

    def test_timeout_block_is_optional_text_input(self):
        modal = build_create_task_modal("lisa")
        timeout_block = next(b for b in modal["blocks"] if b.get("block_id") == BLOCK_ID_TIMEOUT)
        assert timeout_block.get("optional") is True
        assert timeout_block["element"]["type"] == "plain_text_input"
        # Placeholder should mention the default so users know what happens if left blank.
        placeholder_text = timeout_block["element"]["placeholder"]["text"]
        assert "300" in placeholder_text


@pytest.mark.unit
class TestParseSubmission:
    def _base_view(self, timeout_value=""):
        view = {
            "private_metadata": "lisa",
            "state": {
                "values": {
                    BLOCK_ID_NAME: {ACTION_ID_NAME: {"value": "Review"}},
                    "task_prompt": {"prompt_input": {"value": "Do it"}},
                    BLOCK_ID_CRON: {ACTION_ID_CRON: {"value": "0 9 * * 1-5"}},
                    BLOCK_ID_DESTINATION: {"destination_input": {"selected_conversation": "C_DEST"}},
                }
            },
        }
        if timeout_value:
            view["state"]["values"][BLOCK_ID_TIMEOUT] = {"timeout_input": {"value": timeout_value}}
        return view

    def test_roundtrip(self):
        view = {
            "private_metadata": "lisa",
            "state": {
                "values": {
                    BLOCK_ID_NAME: {ACTION_ID_NAME: {"value": "Review"}},
                    "task_prompt": {"prompt_input": {"value": "Do it"}},
                    BLOCK_ID_CRON: {ACTION_ID_CRON: {"value": "0 9 * * 1-5"}},
                    # conversations_select payloads carry the picked channel
                    # under `selected_conversation`, not `value`.
                    BLOCK_ID_DESTINATION: {"destination_input": {"selected_conversation": "C_DEST"}},
                }
            },
        }
        parsed = parse_create_modal_submission(view)
        assert parsed == {
            "agent_name": "lisa",
            "name": "Review",
            "prompt": "Do it",
            "schedule_cron": "0 9 * * 1-5",
            "destination": "C_DEST",
            "timeout_seconds": None,
        }

    def test_missing_destination_becomes_none(self):
        view = {
            "private_metadata": "lisa",
            "state": {
                "values": {
                    BLOCK_ID_NAME: {ACTION_ID_NAME: {"value": "Review"}},
                    "task_prompt": {"prompt_input": {"value": "Do it"}},
                    BLOCK_ID_CRON: {ACTION_ID_CRON: {"value": "0 9 * * *"}},
                    BLOCK_ID_DESTINATION: {"destination_input": {}},
                }
            },
        }
        parsed = parse_create_modal_submission(view)
        assert parsed["destination"] is None

    def test_blank_timeout_becomes_none(self):
        parsed = parse_create_modal_submission(self._base_view(timeout_value=""))
        assert parsed["timeout_seconds"] is None

    def test_valid_timeout_is_parsed_as_int(self):
        parsed = parse_create_modal_submission(self._base_view(timeout_value="1800"))
        assert parsed["timeout_seconds"] == 1800

    def test_non_integer_timeout_becomes_sentinel(self):
        # Non-integers parse to -1 so the handler can emit the block error.
        parsed = parse_create_modal_submission(self._base_view(timeout_value="abc"))
        assert parsed["timeout_seconds"] == -1
