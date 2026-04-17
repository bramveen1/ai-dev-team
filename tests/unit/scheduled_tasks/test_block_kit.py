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
        assert "C_DEST" in flat

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


@pytest.mark.unit
class TestParseSubmission:
    def test_roundtrip(self):
        view = {
            "private_metadata": "lisa",
            "state": {
                "values": {
                    BLOCK_ID_NAME: {ACTION_ID_NAME: {"value": "Review"}},
                    "task_prompt": {"prompt_input": {"value": "Do it"}},
                    BLOCK_ID_CRON: {ACTION_ID_CRON: {"value": "0 9 * * 1-5"}},
                    BLOCK_ID_DESTINATION: {"destination_input": {"value": "C_DEST"}},
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
        }

    def test_blank_destination_becomes_none(self):
        view = {
            "private_metadata": "lisa",
            "state": {
                "values": {
                    BLOCK_ID_NAME: {ACTION_ID_NAME: {"value": "Review"}},
                    "task_prompt": {"prompt_input": {"value": "Do it"}},
                    BLOCK_ID_CRON: {ACTION_ID_CRON: {"value": "0 9 * * *"}},
                    BLOCK_ID_DESTINATION: {"destination_input": {"value": "   "}},
                }
            },
        }
        parsed = parse_create_modal_submission(view)
        assert parsed["destination"] is None
