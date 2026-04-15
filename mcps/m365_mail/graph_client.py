"""Microsoft Graph API client for delegate mailbox access.

Wraps the Graph v1.0 REST API for reading messages and managing drafts
in a shared/delegated mailbox. Uses access tokens obtained via OAuth2
device-code or refresh-token flow.

This client intentionally omits any send functionality — the trust boundary
is enforced here at the API client level.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class GraphMailError(Exception):
    """Raised when a Graph API call fails."""

    def __init__(self, status_code: int, message: str, error_code: str | None = None):
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(f"Graph API error {status_code}: {message}")


class GraphMailClient:
    """Delegate-access mail client for Microsoft Graph.

    Args:
        access_token: OAuth2 access token with Mail.Read.Shared and Mail.ReadWrite.Shared scopes.
        user_id: The mailbox owner's UPN or object ID (e.g. bram@pathtohired.com).
                 When provided, uses /users/{user_id}/... endpoints for delegate access.
                 When None, uses /me/... endpoints.
    """

    def __init__(self, access_token: str, user_id: str | None = None) -> None:
        self._token = access_token
        self._user_id = user_id
        self._client = httpx.AsyncClient(
            base_url=GRAPH_BASE,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    @property
    def _base_path(self) -> str:
        """Return the base path for mailbox operations."""
        if self._user_id:
            return f"/users/{quote(self._user_id, safe='@')}"
        return "/me"

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Make an authenticated request to the Graph API."""
        response = await self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            try:
                error_body = response.json()
                error_msg = error_body.get("error", {}).get("message", response.text)
                error_code = error_body.get("error", {}).get("code")
            except (json.JSONDecodeError, KeyError):
                error_msg = response.text
                error_code = None
            raise GraphMailError(response.status_code, error_msg, error_code)
        if response.status_code == 204:
            return {}
        return response.json()

    async def list_messages(
        self,
        folder: str = "inbox",
        top: int = 10,
        filter_expr: str | None = None,
        select: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """List messages from a mail folder.

        Args:
            folder: Mail folder name (inbox, drafts, sentitems, etc.)
            top: Maximum number of messages to return.
            filter_expr: OData $filter expression (e.g. "isRead eq false").
            select: List of fields to include (e.g. ["subject", "from", "receivedDateTime"]).

        Returns:
            List of message objects.
        """
        params: dict[str, str] = {"$top": str(top), "$orderby": "receivedDateTime desc"}
        if filter_expr:
            params["$filter"] = filter_expr
        if select:
            params["$select"] = ",".join(select)

        result = await self._request(
            "GET",
            f"{self._base_path}/mailFolders/{folder}/messages",
            params=params,
        )
        return result.get("value", [])

    async def read_message(self, message_id: str) -> dict[str, Any]:
        """Read a single message by ID.

        Args:
            message_id: The Graph message ID.

        Returns:
            Full message object including body content.
        """
        return await self._request("GET", f"{self._base_path}/messages/{message_id}")

    async def create_draft(
        self,
        subject: str,
        body: str,
        to_recipients: list[str],
        cc_recipients: list[str] | None = None,
        reply_to_message_id: str | None = None,
        body_content_type: str = "HTML",
    ) -> dict[str, Any]:
        """Create a new draft message in the Drafts folder.

        If reply_to_message_id is provided, creates a reply draft to that message.

        Args:
            subject: Email subject.
            body: Email body content.
            to_recipients: List of recipient email addresses.
            cc_recipients: Optional list of CC email addresses.
            reply_to_message_id: If set, create a reply draft to this message.
            body_content_type: Content type of the body ("HTML" or "Text").

        Returns:
            The created draft message object (includes the draft ID).
        """
        if reply_to_message_id:
            # Create a reply draft using the /createReply endpoint
            draft = await self._request(
                "POST",
                f"{self._base_path}/messages/{reply_to_message_id}/createReply",
            )
            # Update the reply draft with the actual content
            draft_id = draft["id"]
            return await self.update_draft(
                draft_id=draft_id,
                subject=subject,
                body=body,
                to_recipients=to_recipients,
                cc_recipients=cc_recipients,
                body_content_type=body_content_type,
            )

        # Create a fresh draft
        message_data: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": body_content_type, "content": body},
            "toRecipients": [{"emailAddress": {"address": addr}} for addr in to_recipients],
        }
        if cc_recipients:
            message_data["ccRecipients"] = [{"emailAddress": {"address": addr}} for addr in cc_recipients]

        return await self._request("POST", f"{self._base_path}/messages", json=message_data)

    async def update_draft(
        self,
        draft_id: str,
        subject: str | None = None,
        body: str | None = None,
        to_recipients: list[str] | None = None,
        cc_recipients: list[str] | None = None,
        body_content_type: str = "HTML",
    ) -> dict[str, Any]:
        """Update an existing draft message.

        Only provided fields are updated; None fields are left unchanged.

        Args:
            draft_id: The Graph message ID of the draft.
            subject: New subject (or None to leave unchanged).
            body: New body content (or None to leave unchanged).
            to_recipients: New recipient list (or None to leave unchanged).
            cc_recipients: New CC list (or None to leave unchanged).
            body_content_type: Content type of the body ("HTML" or "Text").

        Returns:
            The updated draft message object.
        """
        patch_data: dict[str, Any] = {}
        if subject is not None:
            patch_data["subject"] = subject
        if body is not None:
            patch_data["body"] = {"contentType": body_content_type, "content": body}
        if to_recipients is not None:
            patch_data["toRecipients"] = [{"emailAddress": {"address": addr}} for addr in to_recipients]
        if cc_recipients is not None:
            patch_data["ccRecipients"] = [{"emailAddress": {"address": addr}} for addr in cc_recipients]

        return await self._request("PATCH", f"{self._base_path}/messages/{draft_id}", json=patch_data)

    async def delete_draft(self, draft_id: str) -> None:
        """Delete a draft message.

        Args:
            draft_id: The Graph message ID of the draft to delete.
        """
        await self._request("DELETE", f"{self._base_path}/messages/{draft_id}")

    async def get_draft_url(self, draft_id: str) -> str:
        """Generate an Outlook deep link URL for a draft.

        Returns a URL that opens the draft in Outlook web app for manual review/send.

        Args:
            draft_id: The Graph message ID of the draft.

        Returns:
            Outlook web app URL to open the draft.
        """
        # The webLink property on the message gives a direct Outlook Web link
        try:
            message = await self._request(
                "GET",
                f"{self._base_path}/messages/{draft_id}",
                params={"$select": "webLink"},
            )
            if "webLink" in message:
                return message["webLink"]
        except GraphMailError:
            logger.warning("Could not fetch webLink for draft %s, using fallback URL", draft_id)

        # Fallback: construct URL from the draft ID
        return f"https://outlook.office.com/mail/drafts/id/{quote(draft_id, safe='')}"

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
