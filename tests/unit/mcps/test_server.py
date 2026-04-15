"""Tests for the M365 Mail MCP server tool definitions and handler.

Verifies:
- Tool definitions include all required tools and NO send tool
- Tool handler dispatches correctly to GraphMailClient methods
- Tool output is formatted correctly
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcps.m365_mail.server import (
    TOOL_DEFINITIONS,
    _format_message,
    _summarize_messages,
    get_tool_definitions,
    handle_tool_call,
)

pytestmark = pytest.mark.unit


class TestToolDefinitions:
    """Verify the tool surface area is correct — especially that send is absent."""

    def test_expected_tools_present(self):
        tool_names = {t["name"] for t in TOOL_DEFINITIONS}
        expected = {"list_messages", "read_message", "create_draft", "update_draft", "delete_draft", "get_draft_url"}
        assert tool_names == expected

    def test_no_send_tool(self):
        """Enforce the no-send trust boundary at the tool level."""
        tool_names = {t["name"] for t in TOOL_DEFINITIONS}
        send_like = {n for n in tool_names if "send" in n.lower()}
        assert send_like == set(), f"Found send-like tools: {send_like}"

    def test_get_tool_definitions_returns_same(self):
        assert get_tool_definitions() == TOOL_DEFINITIONS

    def test_all_tools_have_input_schema(self):
        for tool in TOOL_DEFINITIONS:
            assert "inputSchema" in tool, f"Tool {tool['name']} missing inputSchema"
            assert tool["inputSchema"]["type"] == "object"

    def test_create_draft_requires_subject_body_to(self):
        create = next(t for t in TOOL_DEFINITIONS if t["name"] == "create_draft")
        required = create["inputSchema"]["required"]
        assert "subject" in required
        assert "body" in required
        assert "to_recipients" in required


class TestHandleToolCall:
    """Verify tool call dispatch to the GraphMailClient."""

    @pytest.fixture
    def mock_client(self):
        client = AsyncMock()
        client.list_messages.return_value = [
            {
                "id": "msg1",
                "subject": "Hello",
                "from": {"emailAddress": {"name": "Alice", "address": "alice@example.com"}},
                "receivedDateTime": "2026-04-15T10:00:00Z",
                "isRead": False,
                "isDraft": False,
                "bodyPreview": "Hi there, this is a test message.",
            }
        ]
        client.read_message.return_value = {
            "id": "msg1",
            "subject": "Hello",
            "from": {"emailAddress": {"name": "Alice", "address": "alice@example.com"}},
            "toRecipients": [{"emailAddress": {"address": "bram@example.com"}}],
            "ccRecipients": [],
            "receivedDateTime": "2026-04-15T10:00:00Z",
            "isRead": False,
            "body": {"contentType": "HTML", "content": "<p>Hello</p>"},
            "hasAttachments": False,
            "isDraft": False,
            "conversationId": "conv1",
        }
        client.create_draft.return_value = {"id": "draft1", "subject": "Re: Hello"}
        client.get_draft_url.return_value = "https://outlook.office.com/mail/drafts/id/draft1"
        client.update_draft.return_value = {"id": "draft1", "subject": "Updated"}
        client.delete_draft.return_value = None
        return client

    @pytest.mark.asyncio
    async def test_list_messages(self, mock_client):
        result = await handle_tool_call(mock_client, "list_messages", {"folder": "inbox", "top": 5})

        mock_client.list_messages.assert_awaited_once()
        assert "messages" in result
        assert len(result["messages"]) == 1
        assert result["messages"][0]["subject"] == "Hello"
        assert "Alice" in result["messages"][0]["from"]

    @pytest.mark.asyncio
    async def test_read_message(self, mock_client):
        result = await handle_tool_call(mock_client, "read_message", {"message_id": "msg1"})

        mock_client.read_message.assert_awaited_once_with("msg1")
        assert result["subject"] == "Hello"
        assert result["to"] == ["bram@example.com"]

    @pytest.mark.asyncio
    async def test_create_draft(self, mock_client):
        result = await handle_tool_call(
            mock_client,
            "create_draft",
            {
                "subject": "Re: Hello",
                "body": "Thanks!",
                "to_recipients": ["alice@example.com"],
            },
        )

        mock_client.create_draft.assert_awaited_once()
        assert result["draft_id"] == "draft1"
        assert result["status"] == "created"
        assert "outlook.office.com" in result["web_link"]

    @pytest.mark.asyncio
    async def test_create_draft_reply(self, mock_client):
        await handle_tool_call(
            mock_client,
            "create_draft",
            {
                "subject": "Re: Hello",
                "body": "Thanks!",
                "to_recipients": ["alice@example.com"],
                "reply_to_message_id": "msg1",
            },
        )

        call_kwargs = mock_client.create_draft.call_args.kwargs
        assert call_kwargs["reply_to_message_id"] == "msg1"

    @pytest.mark.asyncio
    async def test_update_draft(self, mock_client):
        result = await handle_tool_call(
            mock_client,
            "update_draft",
            {"draft_id": "draft1", "subject": "Updated"},
        )

        mock_client.update_draft.assert_awaited_once()
        assert result["status"] == "updated"

    @pytest.mark.asyncio
    async def test_delete_draft(self, mock_client):
        result = await handle_tool_call(mock_client, "delete_draft", {"draft_id": "draft1"})

        mock_client.delete_draft.assert_awaited_once_with("draft1")
        assert result["status"] == "deleted"

    @pytest.mark.asyncio
    async def test_get_draft_url(self, mock_client):
        result = await handle_tool_call(mock_client, "get_draft_url", {"draft_id": "draft1"})

        mock_client.get_draft_url.assert_awaited_once_with("draft1")
        assert "outlook.office.com" in result["url"]

    @pytest.mark.asyncio
    async def test_unknown_tool_raises(self, mock_client):
        with pytest.raises(ValueError, match="Unknown tool"):
            await handle_tool_call(mock_client, "send_message", {})

    @pytest.mark.asyncio
    async def test_send_tool_does_not_exist(self, mock_client):
        """Attempting to call a send-like tool should raise ValueError."""
        for name in ["send", "send_message", "send_mail", "send_email"]:
            with pytest.raises(ValueError, match="Unknown tool"):
                await handle_tool_call(mock_client, name, {})


class TestMessageFormatting:
    def test_summarize_messages(self):
        messages = [
            {
                "id": "m1",
                "subject": "Test",
                "from": {"emailAddress": {"name": "Alice", "address": "alice@ex.com"}},
                "receivedDateTime": "2026-04-15T10:00:00Z",
                "isRead": True,
                "isDraft": False,
                "bodyPreview": "Hello world",
            }
        ]
        result = _summarize_messages(messages)
        assert len(result) == 1
        assert result[0]["id"] == "m1"
        assert result[0]["subject"] == "Test"
        assert "Alice" in result[0]["from"]
        assert result[0]["is_read"] is True

    def test_summarize_messages_no_from(self):
        messages = [{"id": "m1", "subject": "No From"}]
        result = _summarize_messages(messages)
        assert result[0]["from"] == ""

    def test_format_message_full(self):
        message = {
            "id": "m1",
            "subject": "Test",
            "from": {"emailAddress": {"name": "Bob", "address": "bob@ex.com"}},
            "toRecipients": [{"emailAddress": {"address": "alice@ex.com"}}],
            "ccRecipients": [{"emailAddress": {"address": "carol@ex.com"}}],
            "receivedDateTime": "2026-04-15T10:00:00Z",
            "isRead": False,
            "body": {"contentType": "HTML", "content": "<p>Hello</p>"},
            "hasAttachments": True,
            "isDraft": False,
            "conversationId": "conv1",
        }
        result = _format_message(message)
        assert result["id"] == "m1"
        assert result["to"] == ["alice@ex.com"]
        assert result["cc"] == ["carol@ex.com"]
        assert result["has_attachments"] is True
        assert "Bob" in result["from"]
