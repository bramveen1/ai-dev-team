"""M365 Mail MCP server — stdio-based tool server for delegate mailbox access.

Exposes a restricted set of tools for reading and drafting emails via Microsoft Graph.
Intentionally does NOT expose a send tool, enforcing the no-send trust boundary.

Tools:
  - list_messages: List messages from a mail folder
  - read_message: Read a single message by ID
  - create_draft: Create a new draft (or reply draft) in the Drafts folder
  - update_draft: Update an existing draft
  - delete_draft: Delete a draft
  - get_draft_url: Get an Outlook deep link to open a draft for manual send

Environment variables:
  - M365_ACCESS_TOKEN: OAuth2 access token (required)
  - M365_ACCOUNT: Mailbox owner UPN for delegate access (optional, uses /me if unset)
"""

from __future__ import annotations

import logging
from typing import Any

from mcps.m365_mail.graph_client import GraphMailClient

logger = logging.getLogger(__name__)

# Tool definitions — the schema exposed to the LLM via MCP.
# Explicitly: NO send tool.
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_messages",
        "description": (
            "List messages from a mail folder. Returns subject, from, date, and read status. "
            "Use folder='inbox' for inbox, 'drafts' for drafts, 'sentitems' for sent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "description": "Mail folder name (inbox, drafts, sentitems, etc.)",
                    "default": "inbox",
                },
                "top": {
                    "type": "integer",
                    "description": "Maximum number of messages to return (1-50)",
                    "default": 10,
                },
                "filter": {
                    "type": "string",
                    "description": "OData filter expression (e.g. 'isRead eq false')",
                },
            },
        },
    },
    {
        "name": "read_message",
        "description": "Read a single email message by ID. Returns full message including body content.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "The Graph message ID",
                },
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "create_draft",
        "description": (
            "Create a new draft email in the Drafts folder. If reply_to_message_id is provided, "
            "creates a reply draft to that message. The draft must be manually sent by the mailbox owner."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Email subject line"},
                "body": {"type": "string", "description": "Email body content (HTML supported)"},
                "to_recipients": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of recipient email addresses",
                },
                "cc_recipients": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of CC email addresses",
                },
                "reply_to_message_id": {
                    "type": "string",
                    "description": "If set, creates a reply draft to this message ID",
                },
                "body_content_type": {
                    "type": "string",
                    "enum": ["HTML", "Text"],
                    "description": "Content type of the body",
                    "default": "HTML",
                },
            },
            "required": ["subject", "body", "to_recipients"],
        },
    },
    {
        "name": "update_draft",
        "description": "Update an existing draft email. Only provided fields are changed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "draft_id": {"type": "string", "description": "The Graph message ID of the draft"},
                "subject": {"type": "string", "description": "New subject (omit to keep current)"},
                "body": {"type": "string", "description": "New body content (omit to keep current)"},
                "to_recipients": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "New recipient list (omit to keep current)",
                },
                "cc_recipients": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "New CC list (omit to keep current)",
                },
                "body_content_type": {
                    "type": "string",
                    "enum": ["HTML", "Text"],
                    "description": "Content type of the body",
                    "default": "HTML",
                },
            },
            "required": ["draft_id"],
        },
    },
    {
        "name": "delete_draft",
        "description": "Delete a draft email from the Drafts folder.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "draft_id": {"type": "string", "description": "The Graph message ID of the draft to delete"},
            },
            "required": ["draft_id"],
        },
    },
    {
        "name": "get_draft_url",
        "description": (
            "Get an Outlook web app URL to open a draft for manual review and send. "
            "Returns a deep link that the mailbox owner can use to open the draft in Outlook."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "draft_id": {"type": "string", "description": "The Graph message ID of the draft"},
            },
            "required": ["draft_id"],
        },
    },
]


def get_tool_definitions() -> list[dict[str, Any]]:
    """Return the list of tool definitions exposed by this MCP server."""
    return TOOL_DEFINITIONS


async def handle_tool_call(
    client: GraphMailClient,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a tool call to the appropriate Graph client method.

    Args:
        client: Authenticated GraphMailClient instance.
        tool_name: Name of the tool to invoke.
        arguments: Tool arguments from the MCP request.

    Returns:
        Tool result as a dict.

    Raises:
        ValueError: If the tool name is unknown.
        GraphMailError: If the Graph API call fails.
    """
    if tool_name == "list_messages":
        messages = await client.list_messages(
            folder=arguments.get("folder", "inbox"),
            top=arguments.get("top", 10),
            filter_expr=arguments.get("filter"),
            select=["id", "subject", "from", "receivedDateTime", "isRead", "isDraft", "bodyPreview"],
        )
        return {"messages": _summarize_messages(messages)}

    elif tool_name == "read_message":
        message = await client.read_message(arguments["message_id"])
        return _format_message(message)

    elif tool_name == "create_draft":
        draft = await client.create_draft(
            subject=arguments["subject"],
            body=arguments["body"],
            to_recipients=arguments["to_recipients"],
            cc_recipients=arguments.get("cc_recipients"),
            reply_to_message_id=arguments.get("reply_to_message_id"),
            body_content_type=arguments.get("body_content_type", "HTML"),
        )
        url = await client.get_draft_url(draft["id"])
        return {
            "draft_id": draft["id"],
            "web_link": url,
            "subject": draft.get("subject", ""),
            "status": "created",
        }

    elif tool_name == "update_draft":
        draft = await client.update_draft(
            draft_id=arguments["draft_id"],
            subject=arguments.get("subject"),
            body=arguments.get("body"),
            to_recipients=arguments.get("to_recipients"),
            cc_recipients=arguments.get("cc_recipients"),
            body_content_type=arguments.get("body_content_type", "HTML"),
        )
        return {
            "draft_id": draft["id"],
            "subject": draft.get("subject", ""),
            "status": "updated",
        }

    elif tool_name == "delete_draft":
        await client.delete_draft(arguments["draft_id"])
        return {"draft_id": arguments["draft_id"], "status": "deleted"}

    elif tool_name == "get_draft_url":
        url = await client.get_draft_url(arguments["draft_id"])
        return {"draft_id": arguments["draft_id"], "url": url}

    else:
        raise ValueError(f"Unknown tool: {tool_name}")


def _summarize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarize message list for compact tool output."""
    summaries = []
    for msg in messages:
        from_addr = ""
        if "from" in msg and msg["from"]:
            email_addr = msg["from"].get("emailAddress", {})
            from_addr = email_addr.get("address", "")
            from_name = email_addr.get("name", "")
            if from_name:
                from_addr = f"{from_name} <{from_addr}>"

        summaries.append(
            {
                "id": msg.get("id", ""),
                "subject": msg.get("subject", "(no subject)"),
                "from": from_addr,
                "date": msg.get("receivedDateTime", ""),
                "is_read": msg.get("isRead", False),
                "preview": msg.get("bodyPreview", "")[:200],
            }
        )
    return summaries


def _format_message(message: dict[str, Any]) -> dict[str, Any]:
    """Format a full message for tool output."""
    from_addr = ""
    if "from" in message and message["from"]:
        email_data = message["from"].get("emailAddress", {})
        from_addr = email_data.get("address", "")
        from_name = email_data.get("name", "")
        if from_name:
            from_addr = f"{from_name} <{from_addr}>"

    to_addrs = []
    for r in message.get("toRecipients", []):
        addr = r.get("emailAddress", {}).get("address", "")
        if addr:
            to_addrs.append(addr)

    cc_addrs = []
    for r in message.get("ccRecipients", []):
        addr = r.get("emailAddress", {}).get("address", "")
        if addr:
            cc_addrs.append(addr)

    body = message.get("body", {})
    return {
        "id": message.get("id", ""),
        "subject": message.get("subject", "(no subject)"),
        "from": from_addr,
        "to": to_addrs,
        "cc": cc_addrs,
        "date": message.get("receivedDateTime", ""),
        "is_read": message.get("isRead", False),
        "body_type": body.get("contentType", ""),
        "body": body.get("content", ""),
        "has_attachments": message.get("hasAttachments", False),
        "is_draft": message.get("isDraft", False),
        "conversation_id": message.get("conversationId", ""),
    }
